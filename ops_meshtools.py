# B2U Material Master. A full pipeline addon for authoring vertex data and LUT driven materials in Unity
# Copyright (C) 2026 DNArt
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. 
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/>.

import math
import struct
import bpy
import bmesh
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader
from mathutils import Vector, kdtree
import mathutils.bvhtree

# Global state for viewport draw handlers and mode transitions
flow_draw_handle = None
grad_draw_handle = None
last_mode = 'OBJECT'

SHADER_3D_SMOOTH_COLOR = '3D_SMOOTH_COLOR' if bpy.app.version < (3, 0, 0) else 'SMOOTH_COLOR'

WORKFLOW_IDS = {
    'OPAQUE': 0.0,
    'OPAQUE_DS': 0.1,
    'TRANSPARENT': 0.2,
    'CRYSTAL': 0.3,
    'LIQUID': 0.4,
}


# region Helper Functions

def pack_two_floats_to_one(val1, val2):
    """Packs two 32-bit floats as 16-bit half-floats inside a single 32-bit IEEE 754 float."""
    f1 = np.float16(val1)
    f2 = np.float16(val2)
    i1 = int(f1.view(np.uint16))
    i2 = int(f2.view(np.uint16))
    packed_int = (i1 << 16) | i2
    return struct.unpack('f', struct.pack('I', packed_int))[0]


def pack_liquid_flags(obj, start_row):
    """Packs the liquid material row index (bits 0-7) and mesh-wide boolean flags (bits 8-16)."""
    packed_id = int(start_row) & 255
    if getattr(obj, "b2u_liq_tessellated", False):
        packed_id |= (1 << 8)
    if getattr(obj, "b2u_liq_volumetric", False):
        packed_id |= (1 << 9)
    if getattr(obj, "b2u_liq_displacement", False):
        packed_id |= (1 << 10)
    if getattr(obj, "b2u_liq_transmissive", False):
        packed_id |= (1 << 11)
    if getattr(obj, "b2u_liq_ignore_gravity", False):
        packed_id |= (1 << 12)
    if getattr(obj, "b2u_liq_caustics", False):
        packed_id |= (1 << 13)
    if getattr(obj, "b2u_liq_invert_height", False):
        packed_id |= (1 << 14)
    if getattr(obj, "b2u_liq_height_transmission", False):
        packed_id |= (1 << 15)
    if getattr(obj, "b2u_liq_height_emission", False):
        packed_id |= (1 << 16)
    return float(packed_id)


def ensure_vertex_color_layer(mesh, layer_name="B2U_VertexData"):
    """Ensures the target BYTE_COLOR corner attribute exists on the mesh and returns it."""
    if layer_name not in mesh.color_attributes:
        mesh.color_attributes.new(name=layer_name, type='BYTE_COLOR', domain='CORNER')
    layer = mesh.color_attributes[layer_name]
    mesh.color_attributes.active_color = layer
    return layer


def compute_flow_tangent_basis(mesh, is_liquid):
    """Computes and stores B2U_Tangent and B2U_Bitangent corner attributes on the mesh."""
    if not is_liquid:
        try:
            mesh.calc_tangents()
        except RuntimeError:
            pass

    if "B2U_Tangent" not in mesh.attributes:
        mesh.attributes.new(name="B2U_Tangent", type='FLOAT_VECTOR', domain='CORNER')
    if "B2U_Bitangent" not in mesh.attributes:
        mesh.attributes.new(name="B2U_Bitangent", type='FLOAT_VECTOR', domain='CORNER')

    num_loops = len(mesh.loops)
    if num_loops == 0:
        return

    if is_liquid:
        num_verts = len(mesh.vertices)
        vert_normals = np.empty(num_verts * 3, dtype=np.float32)
        mesh.vertices.foreach_get("normal", vert_normals)
        vert_normals.shape = (num_verts, 3)

        loop_vert_indices = np.empty(num_loops, dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", loop_vert_indices)

        normals = vert_normals[loop_vert_indices]
        norms = np.linalg.norm(normals, axis=1, keepdims=True)
        normals = np.divide(normals, norms, out=np.zeros_like(normals), where=norms > 1e-6)

        nx = normals[:, 0]
        ny = normals[:, 1]
        nz = normals[:, 2]

        tangents = np.empty_like(normals)
        bitangents = np.empty_like(normals)

        # Shortest-arc (stereographic) basis from +Z (0, 0, 1) to N; continuous across top, sloped, and vertical faces
        valid = nz > -0.9999
        a = np.zeros(num_loops, dtype=np.float32)
        a[valid] = 1.0 / (1.0 + nz[valid])
        b = -a * nx * ny

        tangents[valid, 0] = 1.0 - a[valid] * (nx[valid] * nx[valid])
        tangents[valid, 1] = b[valid]
        tangents[valid, 2] = -nx[valid]

        bitangents[valid, 0] = b[valid]
        bitangents[valid, 1] = 1.0 - a[valid] * (ny[valid] * ny[valid])
        bitangents[valid, 2] = -ny[valid]

        # Fallback only for upside-down ceiling faces pointing straight down (-Z)
        inv = ~valid
        if np.any(inv):
            tangents[inv] = [0.0, -1.0, 0.0]
            bitangents[inv] = [-1.0, 0.0, 0.0]
    else:
        tangents = np.empty(num_loops * 3, dtype=np.float32)
        bitangents = np.empty(num_loops * 3, dtype=np.float32)
        mesh.loops.foreach_get("tangent", tangents)
        mesh.loops.foreach_get("bitangent", bitangents)

    mesh.attributes["B2U_Tangent"].data.foreach_set("vector", tangents.ravel())
    mesh.attributes["B2U_Bitangent"].data.foreach_set("vector", bitangents.ravel())


def apply_to_selection(context, channel_idx, value):
    """Writes a single RGBA channel value to the selected vertices, edges, or faces in Edit Mode."""
    obj = context.active_object
    mesh = obj.data
    ensure_vertex_color_layer(mesh, "B2U_VertexData")

    bm = bmesh.from_edit_mesh(mesh)
    color_layer = bm.loops.layers.color.get("B2U_VertexData")
    if not color_layer:
        color_layer = bm.loops.layers.color.new("B2U_VertexData")

    selected_loops = set()
    if 'FACE' in bm.select_mode:
        for f in bm.faces:
            if f.select:
                selected_loops.update(f.loops)
    elif 'EDGE' in bm.select_mode:
        for e in bm.edges:
            if e.select:
                selected_loops.update(e.link_loops)
    else:
        for v in bm.verts:
            if v.select:
                selected_loops.update(v.link_loops)

    for loop in selected_loops:
        c = list(loop[color_layer])
        c[channel_idx] = value
        loop[color_layer] = tuple(c)

    bmesh.update_edit_mesh(mesh)


def apply_to_object(context, channel_idx, value):
    """Writes a single RGBA channel value across the entire mesh in either Edit or Object Mode."""
    obj = context.active_object
    mesh = obj.data
    ensure_vertex_color_layer(mesh, "B2U_VertexData")

    if context.mode == 'EDIT_MESH':
        bm = bmesh.from_edit_mesh(mesh)
        color_layer_bm = bm.loops.layers.color.get("B2U_VertexData")
        if not color_layer_bm:
            color_layer_bm = bm.loops.layers.color.new("B2U_VertexData")

        for face in bm.faces:
            for loop in face.loops:
                c = list(loop[color_layer_bm])
                c[channel_idx] = value
                loop[color_layer_bm] = tuple(c)
        bmesh.update_edit_mesh(mesh)
    else:
        color_layer = mesh.color_attributes.get("B2U_VertexData")
        num_loops = len(color_layer.data)
        if num_loops > 0:
            colors = np.empty(num_loops * 4, dtype=np.float32)
            color_layer.data.foreach_get("color", colors)
            colors[channel_idx::4] = value
            color_layer.data.foreach_set("color", colors)
        mesh.update()


def build_blender_preview_lut():
    """Rebuilds the internal float-buffer LUT images used by the Blender viewport preview shaders."""
    scene = bpy.context.scene
    if not hasattr(scene, "b2u_materials"):
        return
    settings = scene.b2u_settings

    if not settings.enable_live_preview:
        return

    num_subtypes = settings.num_subtypes
    base_color_cols = 8 * num_subtypes
    lut_width = base_color_cols + 8
    prop_width = 4

    color_name = "B2U_Preview_ColorLUT"
    prop_name = "B2U_Preview_PropLUT"

    if color_name not in bpy.data.images:
        img_color = bpy.data.images.new(name=color_name, width=lut_width, height=256, alpha=True, float_buffer=True)
    else:
        img_color = bpy.data.images[color_name]
        if img_color.size[0] != lut_width or img_color.size[1] != 256:
            img_color.scale(lut_width, 256)

    if prop_name not in bpy.data.images:
        img_prop = bpy.data.images.new(name=prop_name, width=prop_width, height=256, alpha=True, float_buffer=True)
    else:
        img_prop = bpy.data.images[prop_name]
        if img_prop.size[0] != prop_width or img_prop.size[1] != 256:
            img_prop.scale(prop_width, 256)

    img_color.colorspace_settings.name = 'Non-Color'
    img_prop.colorspace_settings.name = 'Non-Color'
    img_color.alpha_mode = 'CHANNEL_PACKED'
    img_prop.alpha_mode = 'CHANNEL_PACKED'

    pixels_color = [0.0] * (lut_width * 256 * 4)
    pixels_prop = [0.0] * (prop_width * 256 * 4)

    schema_cache = {}
    enum_max_indices = {}
    for s in settings.slot_defs:
        if s.is_active:
            key = (s.schema, s.slot_id)
            schema_cache[key] = s
            if s.data_type == 'ENUM':
                options = [n for n in s.enum_items.split(',') if n.strip()]
                enum_max_indices[key] = max(1.0, float(len(options) - 1))

    def get_norm(mat, schema, slot):
        key = (schema, slot)
        s_def = schema_cache.get(key)
        if not s_def:
            return 0.0

        block = mat.data_blocks.get(slot)
        if not block:
            return 0.0

        dt = s_def.data_type
        if dt == 'FLOAT':
            val = block.f_val
        elif dt == 'INT':
            val = float(block.i_val)
        elif dt == 'BOOL':
            return 1.0 if block.b_val else 0.0
        elif dt == 'BITMASK':
            return min(max(float(block.i_val) / 255.0, 0.0), 1.0)
        elif dt == 'ENUM':
            try:
                current_idx = float(block.e_val)
            except (ValueError, TypeError):
                current_idx = float(block.get("e_val", 0))
            return min(max(current_idx / enum_max_indices[key], 0.0), 1.0)
        else:
            return 0.0

        range_v = s_def.max_val - s_def.min_val
        if range_v == 0.0:
            return 0.0
        return min(max((val - s_def.min_val) / range_v, 0.0), 1.0)

    def write_prop_row(mat, schema, y_pos, is_row_2=False, is_dynamic_row=False):
        prefix = "r2_" if is_row_2 else "r1_"
        row_offset = y_pos * prop_width * 4

        for p_idx in range(3):
            p_num = p_idx + 1
            idx = row_offset + (p_idx * 4)
            pixels_prop[idx:idx + 4] = [
                get_norm(mat, schema, f"{prefix}p{p_num}_r"),
                get_norm(mat, schema, f"{prefix}p{p_num}_g"),
                get_norm(mat, schema, f"{prefix}p{p_num}_b"),
                get_norm(mat, schema, f"{prefix}p{p_num}_a"),
            ]

        if is_row_2:
            p4_r = get_norm(mat, schema, "r2_p4_r")
        else:
            p4_r = WORKFLOW_IDS.get(mat.workflow, 0.0) + (0.5 if is_dynamic_row else 0.0)

        p3_idx = row_offset + 12
        pixels_prop[p3_idx:p3_idx + 4] = [
            p4_r,
            get_norm(mat, schema, f"{prefix}p4_g"),
            get_norm(mat, schema, f"{prefix}p4_b"),
            get_norm(mat, schema, f"{prefix}p4_a"),
        ]

    def write_color_row(mat, y_pos, is_dynamic, alpha_base, alpha_emiss, is_liquid_depth=False):
        row_offset = y_pos * lut_width * 4
        lock_to_first_variant = (not is_dynamic) and (mat.workflow in {'LIQUID', 'CRYSTAL'})
        color_collection = mat.dynamic_colors if is_dynamic else mat.variant_colors
        col_len = len(color_collection)

        for b_idx in range(8):
            lookup_idx = 0 if lock_to_first_variant else b_idx
            c_block = color_collection[lookup_idx] if lookup_idx < col_len else None

            for s_idx in range(num_subtypes):
                x = (b_idx * num_subtypes) + s_idx
                c_idx = row_offset + (x * 4)

                if c_block:
                    if is_dynamic:
                        attr_prefix = "depth" if is_liquid_depth else "base"
                        default_col = c_block.depth_0 if is_liquid_depth else c_block.base_0
                        col = getattr(c_block, f"{attr_prefix}_{s_idx}", default_col)
                    else:
                        col = c_block.depth_0 if is_liquid_depth else c_block.base_0
                    pixels_color[c_idx:c_idx + 3] = col[:3]

                pixels_color[c_idx + 3] = alpha_base

        for b_idx in range(8):
            lookup_idx = 0 if lock_to_first_variant else b_idx
            x = base_color_cols + b_idx
            c_idx = row_offset + (x * 4)

            if lookup_idx < col_len:
                c_block = color_collection[lookup_idx]
                emiss_col = c_block.sec_emission if is_liquid_depth else c_block.emission
                pixels_color[c_idx:c_idx + 3] = emiss_col[:3]
            else:
                pixels_color[c_idx:c_idx + 3] = [0.0, 0.0, 0.0]

            pixels_color[c_idx + 3] = alpha_emiss

    for mat in scene.b2u_materials:
        current_y = mat.start_row_index
        if current_y < 0 or current_y >= 256 or (current_y + mat.allocated_rows) > 256:
            continue

        schema = 'OPAQUE' if mat.workflow == 'OPAQUE_DS' else mat.workflow

        if mat.workflow in {'OPAQUE', 'OPAQUE_DS', 'TRANSPARENT', 'CRYSTAL'}:
            is_dyn_base = (mat.biome_type == 'DYNAMIC')
            a1_norm = get_norm(mat, schema, "r1_ca")
            a2_norm = get_norm(mat, schema, "r1_ea")

            write_prop_row(mat, schema, current_y, is_row_2=False, is_dynamic_row=is_dyn_base)
            write_color_row(mat, current_y, is_dyn_base, a1_norm, a2_norm)

            if mat.biome_type == 'INDEP_DYN':
                write_prop_row(mat, schema, current_y + 1, is_row_2=False, is_dynamic_row=True)
                write_color_row(mat, current_y + 1, True, a1_norm, a2_norm)

        elif mat.workflow == 'LIQUID':
            is_dyn_base = (mat.biome_type == 'DYNAMIC')
            r1_ca = get_norm(mat, schema, "r1_ca")
            r1_ea = get_norm(mat, schema, "r1_ea")
            r2_ca = get_norm(mat, schema, "r2_ca")
            r2_ea = get_norm(mat, schema, "r2_ea")

            write_prop_row(mat, schema, current_y, is_row_2=False, is_dynamic_row=is_dyn_base)
            write_prop_row(mat, schema, current_y + 1, is_row_2=True, is_dynamic_row=is_dyn_base)
            write_color_row(mat, current_y, is_dyn_base, r1_ca, r1_ea, is_liquid_depth=False)
            write_color_row(mat, current_y + 1, is_dyn_base, r2_ca, r2_ea, is_liquid_depth=True)

            if mat.biome_type == 'INDEP_DYN' and (current_y + 3) < 256:
                write_prop_row(mat, schema, current_y + 2, is_row_2=False, is_dynamic_row=True)
                write_prop_row(mat, schema, current_y + 3, is_row_2=True, is_dynamic_row=True)
                write_color_row(mat, current_y + 2, True, r1_ca, r1_ea, is_liquid_depth=False)
                write_color_row(mat, current_y + 3, True, r2_ca, r2_ea, is_liquid_depth=True)

    img_color.pixels.foreach_set(pixels_color)
    img_prop.pixels.foreach_set(pixels_prop)

    img_color.update()
    img_prop.update()


def ensure_gn_live_flow():
    """Builds or retrieves the Geometry Nodes tree for real-time curve-guided fluid flow, collision deflection, and interpolated normal curvature."""
    gn_name = "B2U_LiveFlow_Engine"
    if gn_name in bpy.data.node_groups:
        return bpy.data.node_groups[gn_name]

    group = bpy.data.node_groups.new(gn_name, 'GeometryNodeTree')
    group.is_modifier = True

    if hasattr(group, "interface"):
        group.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
        group.interface.new_socket("Flow Curve", in_out='INPUT', socket_type='NodeSocketObject')
        group.interface.new_socket("Flow Strength", in_out='INPUT', socket_type='NodeSocketFloat').default_value = 1.0
        group.interface.new_socket("Use Curve Falloff", in_out='INPUT', socket_type='NodeSocketBool').default_value = False
        group.interface.new_socket("Curve Falloff Dist", in_out='INPUT', socket_type='NodeSocketFloat').default_value = 10.0
        group.interface.new_socket("Collisions", in_out='INPUT', socket_type='NodeSocketCollection')
        group.interface.new_socket("Collision Influence", in_out='INPUT', socket_type='NodeSocketFloat').default_value = 5.0
        group.interface.new_socket("Pressure Factor", in_out='INPUT', socket_type='NodeSocketFloat').default_value = 1.0
        group.interface.new_socket("Surface Hover Threshold", in_out='INPUT', socket_type='NodeSocketFloat').default_value = 1.0
        group.interface.new_socket("Flow Smoothing", in_out='INPUT', socket_type='NodeSocketInt').default_value = 6
        group.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    else:
        group.inputs.new('NodeSocketGeometry', "Geometry")
        group.inputs.new('NodeSocketObject', "Flow Curve")
        group.inputs.new('NodeSocketFloat', "Flow Strength").default_value = 1.0
        group.inputs.new('NodeSocketBool', "Use Curve Falloff").default_value = False
        group.inputs.new('NodeSocketFloat', "Curve Falloff Dist").default_value = 10.0
        group.inputs.new('NodeSocketCollection', "Collisions")
        group.inputs.new('NodeSocketFloat', "Collision Influence").default_value = 5.0
        group.inputs.new('NodeSocketFloat', "Pressure Factor").default_value = 1.0
        group.inputs.new('NodeSocketFloat', "Surface Hover Threshold").default_value = 1.0
        group.inputs.new('NodeSocketInt', "Flow Smoothing").default_value = 6
        group.outputs.new('NodeSocketGeometry', "Geometry")

    nodes = group.nodes
    links = group.links
    in_node = nodes.new('NodeGroupInput')
    out_node = nodes.new('NodeGroupOutput')

    pos_node = nodes.new('GeometryNodeInputPosition')

    # Force Normal onto the POINT domain so all corners of a vertex share a unified smooth vertex normal
    raw_norm_node = nodes.new('GeometryNodeInputNormal')
    norm_node = nodes.new('GeometryNodeFieldOnDomain')
    norm_node.domain = 'POINT'
    norm_node.data_type = 'FLOAT_VECTOR'
    links.new(raw_norm_node.outputs[0], norm_node.inputs['Value'])

    # Store smooth vertex normal on POINT domain so Sample Nearest Surface interpolates it across triangles
    store_mesh_norm = nodes.new('GeometryNodeStoreNamedAttribute')
    store_mesh_norm.data_type = 'FLOAT_VECTOR'
    store_mesh_norm.domain = 'POINT'
    store_mesh_norm.inputs['Name'].default_value = "B2U_TempNorm"
    links.new(in_node.outputs['Geometry'], store_mesh_norm.inputs['Geometry'])
    links.new(norm_node.outputs['Value'], store_mesh_norm.inputs['Value'])

    # 1. Evaluate collision collection proximity
    col_info = nodes.new('GeometryNodeCollectionInfo')
    col_info.inputs['Separate Children'].default_value = True
    col_info.transform_space = 'RELATIVE'
    links.new(in_node.outputs['Collisions'], col_info.inputs['Collection'])

    realize = nodes.new('GeometryNodeRealizeInstances')
    links.new(col_info.outputs['Instances'], realize.inputs['Geometry'])

    prox = nodes.new('GeometryNodeProximity')
    prox.target_element = 'FACES'
    links.new(realize.outputs['Geometry'], prox.inputs['Target'])

    domain_sz = nodes.new('GeometryNodeAttributeDomainSize')
    links.new(realize.outputs['Geometry'], domain_sz.inputs['Geometry'])

    greater_than = nodes.new('FunctionNodeCompare')
    greater_than.data_type = 'INT'
    greater_than.operation = 'GREATER_THAN'
    greater_than.inputs[3].default_value = 0
    links.new(domain_sz.outputs['Point Count'], greater_than.inputs[2])

    switch_dist = nodes.new('GeometryNodeSwitch')
    switch_dist.input_type = 'FLOAT'
    switch_dist.inputs[1].default_value = 9999.0
    links.new(greater_than.outputs[0], switch_dist.inputs['Switch'])
    links.new(prox.outputs['Distance'], switch_dist.inputs[2])

    sub_col_pos = nodes.new('ShaderNodeVectorMath')
    sub_col_pos.operation = 'SUBTRACT'
    links.new(prox.outputs['Position'], sub_col_pos.inputs[0])
    links.new(pos_node.outputs[0], sub_col_pos.inputs[1])

    dot_norm = nodes.new('ShaderNodeVectorMath')
    dot_norm.operation = 'DOT_PRODUCT'
    links.new(sub_col_pos.outputs[0], dot_norm.inputs[0])
    links.new(norm_node.outputs['Value'], dot_norm.inputs[1])

    abs_norm_dist = nodes.new('ShaderNodeMath')
    abs_norm_dist.operation = 'ABSOLUTE'
    links.new(dot_norm.outputs['Value'], abs_norm_dist.inputs[0])

    map_intersect = nodes.new('ShaderNodeMapRange')
    map_intersect.clamp = True
    map_intersect.inputs['From Min'].default_value = 0.0
    map_intersect.inputs['To Min'].default_value = 1.0
    map_intersect.inputs['To Max'].default_value = 0.0
    links.new(abs_norm_dist.outputs[0], map_intersect.inputs['Value'])
    links.new(in_node.outputs['Surface Hover Threshold'], map_intersect.inputs['From Max'])

    map_col_dist = nodes.new('ShaderNodeMapRange')
    map_col_dist.clamp = True
    map_col_dist.inputs['From Min'].default_value = 0.0
    map_col_dist.inputs['To Min'].default_value = 1.0
    map_col_dist.inputs['To Max'].default_value = 0.0
    links.new(switch_dist.outputs[0], map_col_dist.inputs['Value'])
    links.new(in_node.outputs['Collision Influence'], map_col_dist.inputs['From Max'])

    masked_weight = nodes.new('ShaderNodeMath')
    masked_weight.operation = 'MULTIPLY'
    links.new(map_col_dist.outputs['Result'], masked_weight.inputs[0])
    links.new(map_intersect.outputs['Result'], masked_weight.inputs[1])

    raw_dir_col = nodes.new('ShaderNodeVectorMath')
    raw_dir_col.operation = 'NORMALIZE'
    links.new(sub_col_pos.outputs[0], raw_dir_col.inputs[0])

    blur_dir = nodes.new('GeometryNodeBlurAttribute')
    blur_dir.data_type = 'FLOAT_VECTOR'
    links.new(raw_dir_col.outputs[0], blur_dir.inputs['Value'])
    links.new(in_node.outputs['Flow Smoothing'], blur_dir.inputs['Iterations'])

    eval_blur_dir = nodes.new('GeometryNodeFieldOnDomain')
    eval_blur_dir.domain = 'POINT'
    eval_blur_dir.data_type = 'FLOAT_VECTOR'
    links.new(blur_dir.outputs['Value'], eval_blur_dir.inputs['Value'])

    final_dir = nodes.new('ShaderNodeVectorMath')
    final_dir.operation = 'NORMALIZE'
    links.new(eval_blur_dir.outputs['Value'], final_dir.inputs[0])

    blur_weight = nodes.new('GeometryNodeBlurAttribute')
    blur_weight.data_type = 'FLOAT'
    links.new(masked_weight.outputs[0], blur_weight.inputs['Value'])
    links.new(in_node.outputs['Flow Smoothing'], blur_weight.inputs['Iterations'])

    eval_blur_weight = nodes.new('GeometryNodeFieldOnDomain')
    eval_blur_weight.domain = 'POINT'
    eval_blur_weight.data_type = 'FLOAT'
    links.new(blur_weight.outputs['Value'], eval_blur_weight.inputs['Value'])

    # 2. Sample target flow curve tangent, spline factor [0..1], and nearest surface point
    obj_info = nodes.new('GeometryNodeObjectInfo')
    obj_info.transform_space = 'RELATIVE'
    links.new(in_node.outputs['Flow Curve'], obj_info.inputs['Object'])

    store_tmp = nodes.new('GeometryNodeStoreNamedAttribute')
    store_tmp.data_type = 'FLOAT_VECTOR'
    store_tmp.domain = 'POINT'
    store_tmp.inputs['Name'].default_value = "B2U_TempTan"

    curve_tan = nodes.new('GeometryNodeInputTangent')
    links.new(obj_info.outputs['Geometry'], store_tmp.inputs['Geometry'])
    links.new(curve_tan.outputs['Tangent'], store_tmp.inputs['Value'])

    store_fac = nodes.new('GeometryNodeStoreNamedAttribute')
    store_fac.data_type = 'FLOAT'
    store_fac.domain = 'POINT'
    store_fac.inputs['Name'].default_value = "B2U_TempFac"

    spline_param = nodes.new('GeometryNodeSplineParameter')
    links.new(store_tmp.outputs['Geometry'], store_fac.inputs['Geometry'])
    links.new(spline_param.outputs['Factor'], store_fac.inputs['Value'])

    circle = nodes.new('GeometryNodeCurvePrimitiveCircle')
    circle.inputs['Radius'].default_value = 0.001
    circle.inputs['Resolution'].default_value = 16

    c_to_m = nodes.new('GeometryNodeCurveToMesh')
    links.new(store_fac.outputs['Geometry'], c_to_m.inputs['Curve'])
    links.new(circle.outputs['Curve'], c_to_m.inputs['Profile Curve'])

    sample_surf = nodes.new('GeometryNodeSampleNearestSurface')
    sample_surf.data_type = 'FLOAT_VECTOR'
    links.new(c_to_m.outputs['Mesh'], sample_surf.inputs['Mesh'])

    read_tmp = nodes.new('GeometryNodeInputNamedAttribute')
    read_tmp.data_type = 'FLOAT_VECTOR'
    read_tmp.inputs['Name'].default_value = "B2U_TempTan"
    links.new(read_tmp.outputs['Attribute'], sample_surf.inputs['Value'])

    sample_fac = nodes.new('GeometryNodeSampleNearestSurface')
    sample_fac.data_type = 'FLOAT'
    links.new(c_to_m.outputs['Mesh'], sample_fac.inputs['Mesh'])

    read_fac = nodes.new('GeometryNodeInputNamedAttribute')
    read_fac.data_type = 'FLOAT'
    read_fac.inputs['Name'].default_value = "B2U_TempFac"
    links.new(read_fac.outputs['Attribute'], sample_fac.inputs['Value'])

    sample_pos = nodes.new('GeometryNodeSampleNearestSurface')
    sample_pos.data_type = 'FLOAT_VECTOR'
    links.new(c_to_m.outputs['Mesh'], sample_pos.inputs['Mesh'])
    links.new(pos_node.outputs[0], sample_pos.inputs['Value'])

    sub_curve = nodes.new('ShaderNodeVectorMath')
    sub_curve.operation = 'SUBTRACT'
    links.new(sample_pos.outputs[0], sub_curve.inputs[0])
    links.new(pos_node.outputs[0], sub_curve.inputs[1])

    dist_curve = nodes.new('ShaderNodeVectorMath')
    dist_curve.operation = 'LENGTH'
    links.new(sub_curve.outputs[0], dist_curve.inputs[0])

    dir_curve = nodes.new('ShaderNodeVectorMath')
    dir_curve.operation = 'NORMALIZE'
    links.new(sub_curve.outputs[0], dir_curve.inputs[0])

    dot_par = nodes.new('ShaderNodeVectorMath')
    dot_par.operation = 'DOT_PRODUCT'
    links.new(norm_node.outputs['Value'], dot_par.inputs[0])
    links.new(sample_surf.outputs[0], dot_par.inputs[1])

    abs_par = nodes.new('ShaderNodeMath')
    abs_par.operation = 'ABSOLUTE'
    links.new(dot_par.outputs['Value'], abs_par.inputs[0])

    map_radial_sign = nodes.new('ShaderNodeMapRange')
    map_radial_sign.clamp = True
    map_radial_sign.inputs['From Min'].default_value = 0.45
    map_radial_sign.inputs['From Max'].default_value = 0.55
    map_radial_sign.inputs['To Min'].default_value = -1.0
    map_radial_sign.inputs['To Max'].default_value = 1.0
    links.new(sample_fac.outputs[0], map_radial_sign.inputs['Value'])

    scale_dir = nodes.new('ShaderNodeVectorMath')
    scale_dir.operation = 'MULTIPLY'
    links.new(dir_curve.outputs[0], scale_dir.inputs[0])
    links.new(map_radial_sign.outputs['Result'], scale_dir.inputs[1])

    map_blend = nodes.new('ShaderNodeMapRange')
    map_blend.clamp = True
    map_blend.inputs['From Min'].default_value = 0.5
    map_blend.inputs['From Max'].default_value = 0.9
    map_blend.inputs['To Min'].default_value = 0.0
    map_blend.inputs['To Max'].default_value = 1.0
    links.new(abs_par.outputs[0], map_blend.inputs['Value'])

    sub_blend = nodes.new('ShaderNodeVectorMath')
    sub_blend.operation = 'SUBTRACT'
    links.new(scale_dir.outputs[0], sub_blend.inputs[0])
    links.new(sample_surf.outputs[0], sub_blend.inputs[1])

    mul_blend = nodes.new('ShaderNodeVectorMath')
    mul_blend.operation = 'MULTIPLY'
    links.new(sub_blend.outputs[0], mul_blend.inputs[0])
    links.new(map_blend.outputs['Result'], mul_blend.inputs[1])

    add_blend = nodes.new('ShaderNodeVectorMath')
    add_blend.operation = 'ADD'
    links.new(sample_surf.outputs[0], add_blend.inputs[0])
    links.new(mul_blend.outputs[0], add_blend.inputs[1])

    base_flow = nodes.new('ShaderNodeVectorMath')
    base_flow.operation = 'NORMALIZE'
    links.new(add_blend.outputs[0], base_flow.inputs[0])

    map_falloff = nodes.new('ShaderNodeMapRange')
    map_falloff.clamp = True
    map_falloff.inputs['From Min'].default_value = 0.0
    map_falloff.inputs['To Min'].default_value = 1.0
    map_falloff.inputs['To Max'].default_value = 0.0
    links.new(dist_curve.outputs['Value'], map_falloff.inputs['Value'])
    links.new(in_node.outputs['Curve Falloff Dist'], map_falloff.inputs['From Max'])

    switch_falloff = nodes.new('GeometryNodeSwitch')
    switch_falloff.input_type = 'FLOAT'
    switch_falloff.inputs[1].default_value = 1.0
    links.new(in_node.outputs['Use Curve Falloff'], switch_falloff.inputs['Switch'])
    links.new(map_falloff.outputs['Result'], switch_falloff.inputs[2])

    mul_strength = nodes.new('ShaderNodeMath')
    mul_strength.operation = 'MULTIPLY'
    links.new(switch_falloff.outputs[0], mul_strength.inputs[0])
    links.new(in_node.outputs['Flow Strength'], mul_strength.inputs[1])

    scaled_flow = nodes.new('ShaderNodeVectorMath')
    scaled_flow.operation = 'MULTIPLY'
    links.new(base_flow.outputs[0], scaled_flow.inputs[0])
    links.new(mul_strength.outputs[0], scaled_flow.inputs[1])

    # 3. Deflect flow vector around nearby collision geometry
    dot_col = nodes.new('ShaderNodeVectorMath')
    dot_col.operation = 'DOT_PRODUCT'
    links.new(scaled_flow.outputs[0], dot_col.inputs[0])
    links.new(final_dir.outputs[0], dot_col.inputs[1])

    neg_dot = nodes.new('ShaderNodeMath')
    neg_dot.operation = 'MULTIPLY'
    neg_dot.inputs[1].default_value = -1.0
    links.new(dot_col.outputs['Value'], neg_dot.inputs[0])

    mul_weight = nodes.new('ShaderNodeMath')
    mul_weight.operation = 'MULTIPLY'
    links.new(neg_dot.outputs[0], mul_weight.inputs[0])
    links.new(eval_blur_weight.outputs['Value'], mul_weight.inputs[1])

    mul_pres = nodes.new('ShaderNodeMath')
    mul_pres.operation = 'MULTIPLY'
    links.new(mul_weight.outputs[0], mul_pres.inputs[0])
    links.new(in_node.outputs['Pressure Factor'], mul_pres.inputs[1])

    flow_offset = nodes.new('ShaderNodeVectorMath')
    flow_offset.operation = 'MULTIPLY'
    links.new(final_dir.outputs[0], flow_offset.inputs[0])
    links.new(mul_pres.outputs[0], flow_offset.inputs[1])

    final_flow = nodes.new('ShaderNodeVectorMath')
    final_flow.operation = 'ADD'
    links.new(scaled_flow.outputs[0], final_flow.inputs[0])
    links.new(flow_offset.outputs[0], final_flow.inputs[1])

    blur_final_flow = nodes.new('GeometryNodeBlurAttribute')
    blur_final_flow.data_type = 'FLOAT_VECTOR'
    links.new(final_flow.outputs[0], blur_final_flow.inputs['Value'])
    links.new(in_node.outputs['Flow Smoothing'], blur_final_flow.inputs['Iterations'])

    eval_blur_flow = nodes.new('GeometryNodeFieldOnDomain')
    eval_blur_flow.domain = 'POINT'
    eval_blur_flow.data_type = 'FLOAT_VECTOR'
    links.new(blur_final_flow.outputs['Value'], eval_blur_flow.inputs['Value'])

    # 4. Compute Upstream (X) and Downstream (Y) Interpolated Normal Curvature: kappa = dot(dN_interp, F_tan) / ds
    dot_fn = nodes.new('ShaderNodeVectorMath')
    dot_fn.operation = 'DOT_PRODUCT'
    links.new(eval_blur_flow.outputs['Value'], dot_fn.inputs[0])
    links.new(norm_node.outputs['Value'], dot_fn.inputs[1])

    proj_fn = nodes.new('ShaderNodeVectorMath')
    proj_fn.operation = 'MULTIPLY'
    links.new(norm_node.outputs['Value'], proj_fn.inputs[0])
    links.new(dot_fn.outputs['Value'], proj_fn.inputs[1])

    tan_flow_raw = nodes.new('ShaderNodeVectorMath')
    tan_flow_raw.operation = 'SUBTRACT'
    links.new(eval_blur_flow.outputs['Value'], tan_flow_raw.inputs[0])
    links.new(proj_fn.outputs[0], tan_flow_raw.inputs[1])

    tan_flow_dir = nodes.new('ShaderNodeVectorMath')
    tan_flow_dir.operation = 'NORMALIZE'
    links.new(tan_flow_raw.outputs[0], tan_flow_dir.inputs[0])

    # Step 0.25m along the tangent flow vector to sample the triangle's interpolated normal
    probe_step = nodes.new('ShaderNodeVectorMath')
    probe_step.operation = 'MULTIPLY'
    probe_step.inputs[1].default_value = (0.25, 0.25, 0.25)
    links.new(tan_flow_dir.outputs[0], probe_step.inputs[0])

    probe_pos_up = nodes.new('ShaderNodeVectorMath')
    probe_pos_up.operation = 'SUBTRACT'
    links.new(pos_node.outputs[0], probe_pos_up.inputs[0])
    links.new(probe_step.outputs[0], probe_pos_up.inputs[1])

    probe_pos_down = nodes.new('ShaderNodeVectorMath')
    probe_pos_down.operation = 'ADD'
    links.new(pos_node.outputs[0], probe_pos_down.inputs[0])
    links.new(probe_step.outputs[0], probe_pos_down.inputs[1])

    read_mesh_norm = nodes.new('GeometryNodeInputNamedAttribute')
    read_mesh_norm.data_type = 'FLOAT_VECTOR'
    read_mesh_norm.inputs['Name'].default_value = "B2U_TempNorm"

    def build_normal_rate_branch(probe_pos_out, is_downstream):
        """Measures the rate of change of the interpolated normal along +F_tan."""
        sample_n = nodes.new('GeometryNodeSampleNearestSurface')
        sample_n.data_type = 'FLOAT_VECTOR'
        links.new(store_mesh_norm.outputs['Geometry'], sample_n.inputs['Mesh'])
        links.new(read_mesh_norm.outputs['Attribute'], sample_n.inputs['Value'])
        links.new(probe_pos_out, sample_n.inputs['Sample Position'])

        sample_p = nodes.new('GeometryNodeSampleNearestSurface')
        sample_p.data_type = 'FLOAT_VECTOR'
        links.new(store_mesh_norm.outputs['Geometry'], sample_p.inputs['Mesh'])
        links.new(pos_node.outputs[0], sample_p.inputs['Value'])
        links.new(probe_pos_out, sample_p.inputs['Sample Position'])

        n_interp = nodes.new('ShaderNodeVectorMath')
        n_interp.operation = 'NORMALIZE'
        links.new(sample_n.outputs[0], n_interp.inputs[0])

        # Forward difference along +F_tan: (N_down - N) for downstream, (N - N_up) for upstream
        dn = nodes.new('ShaderNodeVectorMath')
        dn.operation = 'SUBTRACT'
        if is_downstream:
            links.new(n_interp.outputs[0], dn.inputs[0])
            links.new(norm_node.outputs['Value'], dn.inputs[1])
        else:
            links.new(norm_node.outputs['Value'], dn.inputs[0])
            links.new(n_interp.outputs[0], dn.inputs[1])

        dot_dn_f = nodes.new('ShaderNodeVectorMath')
        dot_dn_f.operation = 'DOT_PRODUCT'
        links.new(dn.outputs[0], dot_dn_f.inputs[0])
        links.new(tan_flow_dir.outputs[0], dot_dn_f.inputs[1])

        dp = nodes.new('ShaderNodeVectorMath')
        dp.operation = 'SUBTRACT'
        links.new(sample_p.outputs[0], dp.inputs[0])
        links.new(pos_node.outputs[0], dp.inputs[1])

        ds = nodes.new('ShaderNodeVectorMath')
        ds.operation = 'LENGTH'
        links.new(dp.outputs[0], ds.inputs[0])

        safe_ds = nodes.new('ShaderNodeMath')
        safe_ds.operation = 'MAXIMUM'
        safe_ds.inputs[1].default_value = 0.05
        links.new(ds.outputs['Value'], safe_ds.inputs[0])

        kappa_raw = nodes.new('ShaderNodeMath')
        kappa_raw.operation = 'DIVIDE'
        links.new(dot_dn_f.outputs['Value'], kappa_raw.inputs[0])
        links.new(safe_ds.outputs[0], kappa_raw.inputs[1])

        kappa_clamp = nodes.new('ShaderNodeMapRange')
        kappa_clamp.clamp = True
        kappa_clamp.inputs['From Min'].default_value = -4.0
        kappa_clamp.inputs['From Max'].default_value = 4.0
        kappa_clamp.inputs['To Min'].default_value = -4.0
        kappa_clamp.inputs['To Max'].default_value = 4.0
        links.new(kappa_raw.outputs[0], kappa_clamp.inputs['Value'])
        return kappa_clamp.outputs['Result']

    kappa_up = build_normal_rate_branch(probe_pos_up.outputs[0], is_downstream=False)
    kappa_down = build_normal_rate_branch(probe_pos_down.outputs[0], is_downstream=True)

    comb_curv_raw = nodes.new('ShaderNodeCombineXYZ')
    links.new(kappa_up, comb_curv_raw.inputs[0])
    links.new(kappa_down, comb_curv_raw.inputs[1])

    blur_curv = nodes.new('GeometryNodeBlurAttribute')
    blur_curv.data_type = 'FLOAT_VECTOR'
    links.new(comb_curv_raw.outputs[0], blur_curv.inputs['Value'])
    links.new(in_node.outputs['Flow Smoothing'], blur_curv.inputs['Iterations'])

    # Force POINT evaluation so all corners of a shared vertex write identical curvature values into CORNER UV
    eval_point_curv = nodes.new('GeometryNodeFieldOnDomain')
    eval_point_curv.domain = 'POINT'
    eval_point_curv.data_type = 'FLOAT_VECTOR'
    links.new(blur_curv.outputs['Value'], eval_point_curv.inputs['Value'])

    # 5. Project smoothed 3D flow vector onto mesh tangent/bitangent basis
    read_tan = nodes.new('GeometryNodeInputNamedAttribute')
    read_tan.data_type = 'FLOAT_VECTOR'
    read_tan.inputs['Name'].default_value = "B2U_Tangent"

    read_bitan = nodes.new('GeometryNodeInputNamedAttribute')
    read_bitan.data_type = 'FLOAT_VECTOR'
    read_bitan.inputs['Name'].default_value = "B2U_Bitangent"

    dot_u = nodes.new('ShaderNodeVectorMath')
    dot_u.operation = 'DOT_PRODUCT'
    links.new(eval_blur_flow.outputs['Value'], dot_u.inputs[0])
    links.new(read_tan.outputs[0], dot_u.inputs[1])

    dot_v = nodes.new('ShaderNodeVectorMath')
    dot_v.operation = 'DOT_PRODUCT'
    links.new(eval_blur_flow.outputs['Value'], dot_v.inputs[0])
    links.new(read_bitan.outputs[0], dot_v.inputs[1])

    read_liq = nodes.new('GeometryNodeInputNamedAttribute')
    read_liq.data_type = 'FLOAT_VECTOR'
    read_liq.inputs['Name'].default_value = "B2U_LiquidData"

    sep_liq = nodes.new('ShaderNodeSeparateXYZ')
    links.new(read_liq.outputs['Attribute'], sep_liq.inputs[0])

    comb_flow = nodes.new('ShaderNodeCombineXYZ')
    links.new(dot_u.outputs['Value'], comb_flow.inputs[0])
    links.new(dot_v.outputs['Value'], comb_flow.inputs[1])

    comb_data = nodes.new('ShaderNodeCombineXYZ')
    links.new(switch_dist.outputs[0], comb_data.inputs[0])
    links.new(sep_liq.outputs[1], comb_data.inputs[1])

    store_flow = nodes.new('GeometryNodeStoreNamedAttribute')
    store_flow.data_type = 'FLOAT2'
    store_flow.domain = 'CORNER'
    store_flow.inputs['Name'].default_value = "B2U_LiquidFlow"

    store_curv = nodes.new('GeometryNodeStoreNamedAttribute')
    store_curv.data_type = 'FLOAT2'
    store_curv.domain = 'CORNER'
    store_curv.inputs['Name'].default_value = "B2U_LiquidCurvature"

    store_data = nodes.new('GeometryNodeStoreNamedAttribute')
    store_data.data_type = 'FLOAT2'
    store_data.domain = 'CORNER'
    store_data.inputs['Name'].default_value = "B2U_LiquidData"

    remove_temp_norm = nodes.new('GeometryNodeRemoveAttribute')
    remove_temp_norm.inputs['Name'].default_value = "B2U_TempNorm"

    links.new(store_mesh_norm.outputs['Geometry'], store_flow.inputs['Geometry'])
    links.new(comb_flow.outputs[0], store_flow.inputs['Value'])

    links.new(store_flow.outputs['Geometry'], store_curv.inputs['Geometry'])
    links.new(eval_point_curv.outputs['Value'], store_curv.inputs['Value'])

    links.new(store_curv.outputs['Geometry'], store_data.inputs['Geometry'])
    links.new(comb_data.outputs[0], store_data.inputs['Value'])

    links.new(store_data.outputs['Geometry'], remove_temp_norm.inputs['Geometry'])
    links.new(remove_temp_norm.outputs['Geometry'], out_node.inputs['Geometry'])

    return group

# endregion


# region Material Assigning

class B2U_OT_assign_material_id(bpy.types.Operator):
    """Assigns the active palette material to the selected faces in Edit Mode"""
    bl_idname = "b2u.assign_material_faces"
    bl_label = "Assign Material ID to Faces"
    bl_description = "Assigns the active material to the selected faces. A whole face must be selected, edges and vertices cannot be assigned"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object
            and context.active_object.type == 'MESH'
            and context.mode == 'EDIT_MESH'
            and context.active_object.b2u_workflow != 'NONE'
        )

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.b2u_settings

        mat_id_str = settings.active_assigner_mat_id
        if mat_id_str == "-1":
            self.report({'WARNING'}, "No valid materials for this workflow!")
            return {'CANCELLED'}

        mat = context.scene.b2u_materials[int(mat_id_str)]

        start_row = mat.start_row_index
        if mat.biome_type == 'INDEP_DYN' and settings.assigner_biome_response == 'DYNAMIC':
            start_row += 2 if obj.b2u_workflow == 'LIQUID' else 1

        mesh = obj.data
        bm = bmesh.from_edit_mesh(mesh)
        faces_modified = 0

        if obj.b2u_workflow == 'LIQUID':
            uv_layer = bm.loops.layers.uv.get("B2U_LiquidData")
            if not uv_layer:
                uv_layer = bm.loops.layers.uv.new("B2U_LiquidData")

            packed_v = pack_liquid_flags(obj, start_row)

            for face in bm.faces:
                if face.select:
                    faces_modified += 1
                    for loop in face.loops:
                        loop[uv_layer].uv = (loop[uv_layer].uv[0], packed_v)

            bmesh.update_edit_mesh(mesh)
        else:
            encoded_id = start_row / 255.0
            layer_name = "B2U_VertexData"
            ensure_vertex_color_layer(mesh, layer_name)

            color_layer_bm = bm.loops.layers.color.get(layer_name)
            if not color_layer_bm:
                color_layer_bm = bm.loops.layers.color.new(layer_name)

            apply_variants = obj.b2u_workflow not in {'CRYSTAL', 'LIQUID'}
            if apply_variants:
                variant_idx = int(settings.assigner_variant)
                sign_x = -1.0 if (variant_idx & 1) else 1.0
                sign_y = -1.0 if (variant_idx & 2) else 1.0
                sign_z = -1.0 if (variant_idx & 4) else 1.0

                uv_xy = bm.loops.layers.uv.get("B2U_EdgeDistances_XY")
                uv_zw = bm.loops.layers.uv.get("B2U_EdgeDistances_ZW")
                if not (uv_xy and uv_zw):
                    apply_variants = False

            for face in bm.faces:
                if face.select:
                    faces_modified += 1
                    for loop in face.loops:
                        current_color = loop[color_layer_bm]
                        loop[color_layer_bm] = (current_color[0], current_color[1], current_color[2], encoded_id)

                        if apply_variants:
                            c_xy = loop[uv_xy].uv
                            c_zw = loop[uv_zw].uv

                            dist_x = max(abs(c_xy[0]), 0.0001)
                            dist_y = max(abs(c_xy[1]), 0.0001)
                            dist_z = max(abs(c_zw[0]), 0.0001)

                            loop[uv_xy].uv = (math.copysign(dist_x, sign_x), math.copysign(dist_y, sign_y))
                            loop[uv_zw].uv = (math.copysign(dist_z, sign_z), c_zw[1])

            bmesh.update_edit_mesh(mesh)

        self.report({'INFO'}, f"Assigned Row ID [{start_row}] to {faces_modified} faces!")
        return {'FINISHED'}


class B2U_OT_fill_material_id(bpy.types.Operator):
    """Assigns the active palette material and variant/liquid properties across the entire mesh"""
    bl_idname = "b2u.fill_material_id"
    bl_label = "Fill Object Material"
    bl_description = "Sets the Material ID and Mesh Properties of the entire mesh"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object
            and context.active_object.type == 'MESH'
            and context.active_object.b2u_workflow != 'NONE'
        )

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.b2u_settings

        mat_id_str = settings.active_assigner_mat_id
        if mat_id_str == "-1":
            self.report({'WARNING'}, "No valid materials for this workflow!")
            return {'CANCELLED'}

        mat = context.scene.b2u_materials[int(mat_id_str)]

        start_row = mat.start_row_index
        if mat.biome_type == 'INDEP_DYN' and settings.assigner_biome_response == 'DYNAMIC':
            start_row += 2 if obj.b2u_workflow == 'LIQUID' else 1

        mesh = obj.data

        if obj.b2u_workflow == 'LIQUID':
            packed_v = pack_liquid_flags(obj, start_row)

            is_edit = context.mode == 'EDIT_MESH'
            bm = bmesh.from_edit_mesh(mesh) if is_edit else bmesh.new()
            if not is_edit:
                bm.from_mesh(mesh)

            uv_layer = bm.loops.layers.uv.get("B2U_LiquidData")
            if not uv_layer:
                uv_layer = bm.loops.layers.uv.new("B2U_LiquidData")

            for face in bm.faces:
                for loop in face.loops:
                    loop[uv_layer].uv = (loop[uv_layer].uv[0], packed_v)

            if is_edit:
                bmesh.update_edit_mesh(mesh)
            else:
                bm.to_mesh(mesh)
                mesh.update()
                bm.free()
        else:
            encoded_id = start_row / 255.0
            ensure_vertex_color_layer(mesh, "B2U_VertexData")
            apply_to_object(context, 3, encoded_id)

            if obj.b2u_workflow not in {'CRYSTAL'}:
                variant_idx = int(settings.assigner_variant)
                sign_x = -1.0 if (variant_idx & 1) else 1.0
                sign_y = -1.0 if (variant_idx & 2) else 1.0
                sign_z = -1.0 if (variant_idx & 4) else 1.0

                is_edit = context.mode == 'EDIT_MESH'
                bm2 = bmesh.from_edit_mesh(mesh) if is_edit else bmesh.new()
                if not is_edit:
                    bm2.from_mesh(mesh)

                uv_xy = bm2.loops.layers.uv.get("B2U_EdgeDistances_XY")
                uv_zw = bm2.loops.layers.uv.get("B2U_EdgeDistances_ZW")

                if uv_xy and uv_zw:
                    for face in bm2.faces:
                        for loop in face.loops:
                            c_xy = loop[uv_xy].uv
                            c_zw = loop[uv_zw].uv

                            dist_x = max(abs(c_xy[0]), 0.0001)
                            dist_y = max(abs(c_xy[1]), 0.0001)
                            dist_z = max(abs(c_zw[0]), 0.0001)

                            loop[uv_xy].uv = (math.copysign(dist_x, sign_x), math.copysign(dist_y, sign_y))
                            loop[uv_zw].uv = (math.copysign(dist_z, sign_z), c_zw[1])

                if is_edit:
                    bmesh.update_edit_mesh(mesh)
                else:
                    bm2.to_mesh(mesh)
                    mesh.update()
                    bm2.free()

        self.report({'INFO'}, f"Filled object with Row ID [{start_row}] and applied Mesh Properties!")
        return {'FINISHED'}

# endregion


# region Texture Scale & Grain

class B2U_OT_bake_grain_uvs(bpy.types.Operator):
    """Bakes spherical grain direction and per-face scale into packed 16-bit UV channels"""
    bl_idname = "b2u.bake_grain_uvs"
    bl_label = "Bake Grain & Scale"
    bl_description = "Bakes spherical grain direction and per-face scale to UV channels"
    bl_options = {'REGISTER', 'UNDO'}

    axis: bpy.props.EnumProperty(
        name="Axis",
        items=[
            ('X', "X Axis", ""),
            ('Y', "Y Axis", ""),
            ('Z', "Z Axis", ""),
            ('CUSTOM', "Custom Vector", ""),
        ],
        default='Z',
    )

    azimuth_tweak: bpy.props.FloatProperty(
        name="Azimuth Offset",
        default=0.0,
        description="Rotate the vector horizontally",
        subtype='ANGLE',
    )
    altitude_tweak: bpy.props.FloatProperty(
        name="Altitude Offset",
        default=0.0,
        description="Rotate the vector vertically",
        subtype='ANGLE',
    )

    reverse: bpy.props.BoolProperty(name="Reverse Direction", default=False)
    scale_x: bpy.props.FloatProperty(name="Scale X", default=1.0)
    scale_y: bpy.props.FloatProperty(name="Scale Y", default=1.0)

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data
        settings = context.scene.b2u_settings

        from .data_properties import ensure_uv_maps
        ensure_uv_maps(mesh, obj.b2u_workflow)

        is_edit = (context.mode == 'EDIT_MESH')
        if is_edit:
            bm = bmesh.from_edit_mesh(mesh)
        else:
            bm = bmesh.new()
            bm.from_mesh(mesh)

        uv_grain_scale = bm.loops.layers.uv.get("B2U_GrainAndScale")
        if not uv_grain_scale:
            uv_grain_scale = bm.loops.layers.uv.new("B2U_GrainAndScale")

        ghost_grain = bm.loops.layers.float_vector.get("B2U_Preview_Grain")
        if not ghost_grain:
            ghost_grain = bm.loops.layers.float_vector.new("B2U_Preview_Grain")
        ghost_scale = bm.loops.layers.float_vector.get("B2U_Preview_Scale")
        if not ghost_scale:
            ghost_scale = bm.loops.layers.float_vector.new("B2U_Preview_Scale")

        mw_inv = obj.matrix_world.inverted().to_3x3()

        if self.axis == 'X':
            base_vec = Vector((1.0, 0.0, 0.0))
        elif self.axis == 'Y':
            base_vec = Vector((0.0, 1.0, 0.0))
        elif self.axis == 'Z':
            base_vec = Vector((0.0, 0.0, 1.0))
        elif self.axis == 'CUSTOM':
            base_vec = Vector(settings.grad_custom_end) - Vector(settings.grad_custom_start)
            if base_vec.length > 0.0:
                base_vec.normalize()
            else:
                base_vec = Vector((0.0, 0.0, 1.0))

        packed_scale_y = pack_two_floats_to_one(self.scale_x, self.scale_y)
        preview_scale_vec = (self.scale_x, self.scale_y, 0.0)
        faces_modified = 0

        for face in bm.faces:
            if is_edit and not face.select:
                continue
            faces_modified += 1

            if self.axis == 'AUTO':
                longest_len = -1.0
                vec = Vector((0.0, 0.0, 1.0))
                for edge in face.edges:
                    v = edge.verts[1].co - edge.verts[0].co
                    l = v.length
                    if l > longest_len:
                        longest_len = l
                        vec = v.normalized()

                world_target = Vector((0.0, 0.0, 1.0)) if getattr(self, 'flow_dir', 'UP') == 'UP' else Vector((0.0, 0.0, -1.0))
                local_target = mw_inv @ world_target
                if vec.dot(local_target) < 0.0:
                    vec = -vec
            else:
                vec = base_vec.copy()

            if self.reverse:
                vec = -vec

            theta = math.atan2(vec.y, vec.x) + self.azimuth_tweak
            phi = math.asin(max(min(vec.z, 1.0), -1.0)) + self.altitude_tweak
            phi = max(min(phi, math.pi * 0.5), -math.pi * 0.5)

            vec.x = math.cos(phi) * math.cos(theta)
            vec.y = math.cos(phi) * math.sin(theta)
            vec.z = math.sin(phi)

            u_val = 0.5 if abs(vec.z) > 0.999 else (math.atan2(vec.y, vec.x) / (2.0 * math.pi)) + 0.5
            v_val = (math.asin(max(min(vec.z, 1.0), -1.0)) / math.pi) + 0.5

            packed_grain_x = pack_two_floats_to_one(u_val, v_val)
            preview_grain_vec = (u_val, v_val, 0.0)

            for loop in face.loops:
                loop[uv_grain_scale].uv = (packed_grain_x, packed_scale_y)
                loop[ghost_grain] = preview_grain_vec
                loop[ghost_scale] = preview_scale_vec

        if is_edit:
            bmesh.update_edit_mesh(mesh)
        else:
            bm.to_mesh(mesh)
            mesh.update()
            bm.free()

        self.report({'INFO'}, f"Baked Grain & Scale on {faces_modified} faces!")
        return {'FINISHED'}


class B2U_OT_bake_automatic_grain(bpy.types.Operator):
    """Uses local Principal Component Analysis (PCA) to orient grain along branch geometry"""
    bl_idname = "b2u.bake_automatic_grain"
    bl_label = "Auto-Bake Grain (Volume/PCA)"
    bl_description = "Uses local Principal Component Analysis (PCA) to map the grain direction along the volume of structural branches or geometry"
    bl_options = {'REGISTER', 'UNDO'}

    flow_dir: bpy.props.EnumProperty(
        name="Flow Direction",
        items=[('UP', "Global Up (+Z)", ""), ('DOWN', "Global Down (-Z)", "")],
        default='UP',
    )

    search_radius: bpy.props.FloatProperty(
        name="Volume Radius",
        default=0.5,
        min=0.01,
        description="How far to look when mapping the local volume. Larger values smooth the flow along thick branches, smaller values catch tight corners",
    )

    reverse: bpy.props.BoolProperty(name="Reverse Direction", default=False)
    scale_x: bpy.props.FloatProperty(name="Scale X", default=1.0)
    scale_y: bpy.props.FloatProperty(name="Scale Y", default=1.0)

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data

        from .data_properties import ensure_uv_maps
        ensure_uv_maps(mesh, obj.b2u_workflow)

        mw = obj.matrix_world
        world_target = Vector((0.0, 0.0, 1.0)) if self.flow_dir == 'UP' else Vector((0.0, 0.0, -1.0))
        local_target = mw.inverted().to_3x3() @ world_target

        is_edit = (context.mode == 'EDIT_MESH')
        if is_edit:
            bm = bmesh.from_edit_mesh(mesh)
        else:
            bm = bmesh.new()
            bm.from_mesh(mesh)

        uv_grain_scale = bm.loops.layers.uv.get("B2U_GrainAndScale")
        if not uv_grain_scale:
            uv_grain_scale = bm.loops.layers.uv.new("B2U_GrainAndScale")

        ghost_grain = bm.loops.layers.float_vector.get("B2U_Preview_Grain")
        if not ghost_grain:
            ghost_grain = bm.loops.layers.float_vector.new("B2U_Preview_Grain")
        ghost_scale = bm.loops.layers.float_vector.get("B2U_Preview_Scale")
        if not ghost_scale:
            ghost_scale = bm.loops.layers.float_vector.new("B2U_Preview_Scale")

        kd = kdtree.KDTree(len(bm.faces))
        for i, face in enumerate(bm.faces):
            kd.insert(face.calc_center_median(), i)
        kd.balance()

        packed_scale_y = pack_two_floats_to_one(self.scale_x, self.scale_y)
        preview_scale_vec = (self.scale_x, self.scale_y, 0.0)
        faces_modified = 0

        for face in bm.faces:
            if is_edit and not face.select:
                continue
            faces_modified += 1

            best_vec = Vector((0.0, 0.0, 1.0))
            center = face.calc_center_median()
            neighbors = kd.find_range(center, self.search_radius)

            if len(neighbors) >= 3:
                pts = np.array([n[0] for n in neighbors])
                centroid = np.mean(pts, axis=0)
                centered = pts - centroid
                cov_matrix = np.dot(centered.T, centered)

                evals, evecs = np.linalg.eigh(cov_matrix)
                best_vec = Vector(evecs[:, np.argmax(evals)]).normalized()
            else:
                longest_len = -1.0
                for edge in face.edges:
                    v = edge.verts[1].co - edge.verts[0].co
                    if v.length > longest_len:
                        longest_len = v.length
                        best_vec = v.normalized()

            if best_vec.dot(local_target) < 0.0:
                best_vec = -best_vec

            if self.reverse:
                best_vec = -best_vec

            u_val = 0.5 if abs(best_vec.z) > 0.999 else (math.atan2(best_vec.y, best_vec.x) / (2.0 * math.pi)) + 0.5
            v_val = (math.asin(max(min(best_vec.z, 1.0), -1.0)) / math.pi) + 0.5

            packed_grain_x = pack_two_floats_to_one(u_val, v_val)
            preview_grain_vec = (u_val, v_val, 0.0)

            for loop in face.loops:
                loop[uv_grain_scale].uv = (packed_grain_x, packed_scale_y)
                loop[ghost_grain] = preview_grain_vec
                loop[ghost_scale] = preview_scale_vec

        if is_edit:
            bmesh.update_edit_mesh(mesh)
        else:
            bm.to_mesh(mesh)
            mesh.update()
            bm.free()

        self.report({'INFO'}, f"Auto-Baked Volume PCA Grain & Scale on {faces_modified} faces!")
        return {'FINISHED'}


class B2U_OT_set_grain_vector(bpy.types.Operator):
    """Acquires a custom grain vector from two selected vertices in Edit Mode"""
    bl_idname = "b2u.set_grain_vector"
    bl_label = "Set Grain Direction"
    bl_description = "Select exactly 2 vertices to define the grain flow direction for the active branch"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        bm = bmesh.from_edit_mesh(context.active_object.data)
        sel = [v for v in bm.verts if v.select]
        if len(sel) != 2:
            self.report({'ERROR'}, "Select exactly 2 vertices to define the grain vector!")
            return {'CANCELLED'}

        settings = context.scene.b2u_settings
        settings.grad_custom_start = sel[0].co
        settings.grad_custom_end = sel[1].co
        settings.ui_show_uv_tools = True
        self.report({'INFO'}, "Grain vector acquired!")
        return {'FINISHED'}

# endregion


# region Edge Wear Baking

class B2U_OT_bake_physical_distance(bpy.types.Operator):
    """Bakes per-face sharp-edge distances, edge intensities, and smooth normals into UV channels"""
    bl_idname = "b2u.bake_physical_distance"
    bl_label = "Bake Physical Distance Field"
    bl_description = "Bakes a 5-channel distance and normal field. Strictly enforces Quads and Tris."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data

        # Use obj.mode ('EDIT', 'OBJECT', etc.) rather than context.mode ('EDIT_MESH') 
        # so it matches the enum expected by bpy.ops.object.mode_set()
        original_mode = obj.mode
        if original_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        current_names = {uv.name for uv in mesh.uv_layers}
        is_new_uv = ("B2U_SmoothNormals_ZW" not in current_names)

        from .data_properties import ensure_uv_maps
        ensure_uv_maps(mesh, obj.b2u_workflow)

        bm = bmesh.new()
        bm.from_mesh(mesh)

        ngons = [f for f in bm.faces if len(f.verts) > 4]
        if ngons:
            self.report({'ERROR'}, f"Bake Failed! Found {len(ngons)} N-gons. Edge wear baking requires Quads or Tris to pack data.")
            bm.free()
            if original_mode != 'OBJECT':
                bpy.ops.object.mode_set(mode=original_mode)
            return {'CANCELLED'}

        bm.normal_update()

        uv_dist_xy = bm.loops.layers.uv.get("B2U_EdgeDistances_XY")
        uv_dist_zw = bm.loops.layers.uv.get("B2U_EdgeDistances_ZW")
        uv_int_packed = bm.loops.layers.uv.get("B2U_EdgeIntensities")
        uv_smooth_xy = bm.loops.layers.uv.get("B2U_SmoothNormals_XY")
        uv_smooth_zw = bm.loops.layers.uv.get("B2U_SmoothNormals_ZW")

        ghost_int_xy = bm.loops.layers.float_vector.get("B2U_Preview_Intensities_XY")
        if not ghost_int_xy:
            ghost_int_xy = bm.loops.layers.float_vector.new("B2U_Preview_Intensities_XY")

        ghost_int_zw = bm.loops.layers.float_vector.get("B2U_Preview_Intensities_ZW")
        if not ghost_int_zw:
            ghost_int_zw = bm.loops.layers.float_vector.new("B2U_Preview_Intensities_ZW")

        mw_3x3 = obj.matrix_world.to_3x3()
        angle_limit = context.scene.b2u_settings.edge_wear_angle
        half_pi = math.pi * 0.5

        for face in bm.faces:
            if len(face.verts) < 3:
                continue

            # Transform vertices into scaled world space so physical edge distances are orientation-independent
            v0_w = mw_3x3 @ face.verts[0].co
            v1_w = mw_3x3 @ face.verts[1].co
            x_axis = (v1_w - v0_w).normalized()
            z_axis = (mw_3x3 @ face.normal).normalized()
            y_axis = z_axis.cross(x_axis).normalized()

            local_coords = []
            for v in face.verts:
                diff = (mw_3x3 @ v.co) - v0_w
                local_coords.append(Vector((diff.dot(x_axis), diff.dot(y_axis))))

            edge_normals = []
            edge_origins = []
            edge_intensities = []

            for i, loop in enumerate(face.loops):
                edge = loop.edge
                is_sharp = False
                intensity = 0.0

                if len(edge.link_faces) != 2:
                    is_sharp = True
                    intensity = 1.0
                else:
                    angle = edge.calc_face_angle_signed(0.0)
                    if abs(angle) >= angle_limit:
                        is_sharp = True
                        edge_sign = 1.0 if angle >= 0.0 else -1.0
                        intensity = min(abs(angle) / half_pi, 1.0) * edge_sign

                if is_sharp and len(edge_normals) < 4:
                    pA = local_coords[i]
                    pB = local_coords[(i + 1) % len(local_coords)]
                    edge_dir = pB - pA
                    if edge_dir.length > 1e-6:
                        edge_dir.normalize()
                        edge_normals.append(Vector((-edge_dir.y, edge_dir.x)))
                        edge_origins.append(pA)
                        edge_intensities.append(intensity)

            # Intensities are uniform across all loops of a face; pack once per face
            face_intensities = [
                edge_intensities[e_idx] if e_idx < len(edge_intensities) else 0.0
                for e_idx in range(4)
            ]
            packed_int_x = pack_two_floats_to_one(face_intensities[0], face_intensities[1])
            packed_int_y = pack_two_floats_to_one(face_intensities[2], face_intensities[3])
            preview_int_xy = (face_intensities[0], face_intensities[1], 0.0)
            preview_int_zw = (face_intensities[2], face_intensities[3], 0.0)

            for i, loop in enumerate(face.loops):
                p_v = local_coords[i]

                distances = [
                    abs((p_v - edge_origins[e_idx]).dot(edge_normals[e_idx]))
                    if e_idx < len(edge_normals) else 9999.0
                    for e_idx in range(4)
                ]

                old_xy = loop[uv_dist_xy].uv
                old_zw = loop[uv_dist_zw].uv

                # Preserve existing sign bits used for variant indexing
                loop[uv_dist_xy].uv = (
                    math.copysign(distances[0], old_xy[0]),
                    math.copysign(distances[1], old_xy[1]),
                )
                loop[uv_dist_zw].uv = (
                    math.copysign(distances[2], old_zw[0]),
                    math.copysign(distances[3], old_zw[1]),
                )

                loop[uv_int_packed].uv = (packed_int_x, packed_int_y)
                loop[ghost_int_xy] = preview_int_xy
                loop[ghost_int_zw] = preview_int_zw

                smooth_n = loop.vert.normal.normalized()
                existing_thickness = 1.0 if is_new_uv else loop[uv_smooth_zw].uv[1]

                loop[uv_smooth_xy].uv = ((smooth_n.x * 0.5) + 0.5, (smooth_n.y * 0.5) + 0.5)
                loop[uv_smooth_zw].uv = ((smooth_n.z * 0.5) + 0.5, existing_thickness)

        bm.to_mesh(mesh)
        bm.free()
        mesh.update()

        if original_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=original_mode)

        self.report({'INFO'}, "Successfully Baked Distance & Normal Field!")
        return {'FINISHED'}


class B2U_OT_clear_physical_distance(bpy.types.Operator):
    """Clears the baked edge wear distance field by setting distances to 9999 and intensities to 0"""
    bl_idname = "b2u.clear_physical_distance"
    bl_label = "Clear Distance Field"
    bl_description = "Disables edge wear on this mesh by setting all edge distances to 9999 and intensities to 0 while preserving material variant sign bits"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data

        original_mode = obj.mode
        if original_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        from .data_properties import ensure_uv_maps
        ensure_uv_maps(mesh, obj.b2u_workflow)

        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.normal_update()

        uv_dist_xy = bm.loops.layers.uv.get("B2U_EdgeDistances_XY")
        uv_dist_zw = bm.loops.layers.uv.get("B2U_EdgeDistances_ZW")
        uv_int_packed = bm.loops.layers.uv.get("B2U_EdgeIntensities")
        uv_smooth_xy = bm.loops.layers.uv.get("B2U_SmoothNormals_XY")
        uv_smooth_zw = bm.loops.layers.uv.get("B2U_SmoothNormals_ZW")

        ghost_int_xy = bm.loops.layers.float_vector.get("B2U_Preview_Intensities_XY")
        if not ghost_int_xy:
            ghost_int_xy = bm.loops.layers.float_vector.new("B2U_Preview_Intensities_XY")

        ghost_int_zw = bm.loops.layers.float_vector.get("B2U_Preview_Intensities_ZW")
        if not ghost_int_zw:
            ghost_int_zw = bm.loops.layers.float_vector.new("B2U_Preview_Intensities_ZW")

        packed_zero = pack_two_floats_to_one(0.0, 0.0)
        preview_zero = (0.0, 0.0, 0.0)

        for face in bm.faces:
            for loop in face.loops:
                old_xy = loop[uv_dist_xy].uv
                old_zw = loop[uv_dist_zw].uv

                # Set distances to 9999.0 while preserving sign bits used for variant indexing
                loop[uv_dist_xy].uv = (
                    math.copysign(9999.0, old_xy[0]),
                    math.copysign(9999.0, old_xy[1]),
                )
                loop[uv_dist_zw].uv = (
                    math.copysign(9999.0, old_zw[0]),
                    math.copysign(9999.0, old_zw[1]),
                )

                loop[uv_int_packed].uv = (packed_zero, packed_zero)
                loop[ghost_int_xy] = preview_zero
                loop[ghost_int_zw] = preview_zero

                # Keep smooth normals valid and zero out the edge thickness multiplier
                smooth_n = loop.vert.normal.normalized()
                loop[uv_smooth_xy].uv = ((smooth_n.x * 0.5) + 0.5, (smooth_n.y * 0.5) + 0.5)
                loop[uv_smooth_zw].uv = ((smooth_n.z * 0.5) + 0.5, 0.0)

        bm.to_mesh(mesh)
        bm.free()
        mesh.update()

        if original_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=original_mode)

        self.report({'INFO'}, "Cleared Edge Wear Distance Field (Distances = 9999, Intensities = 0).")
        return {'FINISHED'}


class B2U_OT_apply_edge_thickness(bpy.types.Operator):
    """Paints an edge-wear thickness multiplier into the V channel of B2U_SmoothNormals_ZW"""
    bl_idname = "b2u.apply_edge_thickness"
    bl_label = "Apply Edge Thickness"
    bl_description = "Paints edge thickness into the V channel of B2U_SmoothNormals_ZW."
    bl_options = {'REGISTER', 'UNDO'}

    apply_mode: bpy.props.EnumProperty(
        name="Application Mode",
        items=[
            ('UNIFORM', "Uniform", "Applies the exact value to all edges (Requires Object Mode)"),
            ('ANGLE', "Angle-Based", "Scales the value based on edge severity (Requires Object Mode)"),
            ('MANUAL', "Selected Edges", "Applies the exact value ONLY to selected edges (Requires Edit Mode)"),
        ],
        default='UNIFORM',
    )

    thickness_value: bpy.props.FloatProperty(
        name="Thickness Scale",
        description="The wear thickness multiplier to write (0.0 to 2.0)",
        default=1.0,
        min=0.0,
        max=2.0,
    )

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data
        original_mode = context.mode

        if self.apply_mode in {'UNIFORM', 'ANGLE'} and original_mode != 'OBJECT':
            self.report({'ERROR'}, f"{self.apply_mode} mode must be executed in Object Mode.")
            return {'CANCELLED'}
        if self.apply_mode == 'MANUAL' and original_mode != 'EDIT_MESH':
            self.report({'ERROR'}, "MANUAL mode must be executed in Edit Mode.")
            return {'CANCELLED'}

        target_uv_layer = "B2U_SmoothNormals_ZW"
        if target_uv_layer not in mesh.uv_layers:
            self.report({'ERROR'}, "Distance field must be baked before applying thickness.")
            return {'CANCELLED'}

        if self.apply_mode in {'UNIFORM', 'ANGLE'}:
            bm = bmesh.new()
            bm.from_mesh(mesh)

            if self.apply_mode == 'ANGLE':
                bm.normal_update()

            uv_layer = bm.loops.layers.uv.get(target_uv_layer)

            if self.apply_mode == 'UNIFORM':
                for face in bm.faces:
                    for loop in face.loops:
                        loop[uv_layer].uv = (loop[uv_layer].uv[0], self.thickness_value)

            elif self.apply_mode == 'ANGLE':
                half_pi = math.pi * 0.5
                for face in bm.faces:
                    for loop in face.loops:
                        max_edge_angle = 0.0
                        for e in loop.vert.link_edges:
                            if len(e.link_faces) == 2:
                                angle = abs(e.calc_face_angle_signed(0.0))
                                if angle > max_edge_angle:
                                    max_edge_angle = angle

                        severity_mult = min(max_edge_angle / half_pi, 1.0)
                        loop[uv_layer].uv = (loop[uv_layer].uv[0], self.thickness_value * severity_mult)

            bm.to_mesh(mesh)
            bm.free()
            mesh.update()

        elif self.apply_mode == 'MANUAL':
            bm = bmesh.from_edit_mesh(mesh)
            uv_layer = bm.loops.layers.uv.get(target_uv_layer)
            selected_verts = {v for e in bm.edges if e.select for v in e.verts}

            for face in bm.faces:
                for loop in face.loops:
                    if loop.vert in selected_verts:
                        loop[uv_layer].uv = (loop[uv_layer].uv[0], self.thickness_value)

            bmesh.update_edit_mesh(mesh)

        self.report({'INFO'}, f"Applied {self.apply_mode} thickness to Tangent W: {self.thickness_value:.2f}")
        return {'FINISHED'}

# endregion


# region Paint Masking, Emission & Transparency

class B2U_OT_assign_paint_mask(bpy.types.Operator):
    """Sets the paint mask (Vertex Color B) on the active selection"""
    bl_idname = "b2u.assign_paint_mask"
    bl_label = "Assign Paint Mask"
    bl_description = "Sets the paint mask (Vertex B) of the selected vertices/edges/faces to the value defined."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == 'EDIT_MESH'
            and context.active_object
            and context.active_object.b2u_workflow in {'OPAQUE', 'OPAQUE_DS', 'TRANSPARENT', 'CRYSTAL'}
        )

    def execute(self, context):
        apply_to_selection(context, 2, context.scene.b2u_settings.assigner_paint_val)
        self.report({'INFO'}, "Paint Mask applied to selection!")
        return {'FINISHED'}


class B2U_OT_fill_paint_mask(bpy.types.Operator):
    """Fills the paint mask (Vertex Color B) across the entire mesh"""
    bl_idname = "b2u.fill_paint_mask"
    bl_label = "Fill Object Paint Mask"
    bl_description = "Sets the paint mask (Vertex B) of the entire mesh to the value defined."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object
            and context.active_object.type == 'MESH'
            and context.active_object.b2u_workflow in {'OPAQUE', 'OPAQUE_DS', 'TRANSPARENT', 'CRYSTAL'}
        )

    def execute(self, context):
        apply_to_object(context, 2, context.scene.b2u_settings.assigner_paint_val)
        self.report({'INFO'}, "Filled object with Paint Mask!")
        return {'FINISHED'}


class B2U_OT_assign_emission(bpy.types.Operator):
    """Sets the emission mask (Vertex Color G) on the active selection"""
    bl_idname = "b2u.assign_emission"
    bl_label = "Assign Emission"
    bl_description = "Sets the emission mask (Vertex G) of the selected vertices/edges/faces to the emission value defined."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == 'EDIT_MESH'
            and context.active_object
            and context.active_object.b2u_workflow in {'OPAQUE', 'OPAQUE_DS', 'TRANSPARENT', 'CRYSTAL'}
        )

    def execute(self, context):
        apply_to_selection(context, 1, context.scene.b2u_settings.assigner_emission_val)
        self.report({'INFO'}, "Emission applied to selection!")
        return {'FINISHED'}


class B2U_OT_fill_emission(bpy.types.Operator):
    """Fills the emission mask (Vertex Color G) across the entire mesh"""
    bl_idname = "b2u.fill_emission"
    bl_label = "Fill Object Emission"
    bl_description = "Sets the emission mask (Vertex G) of the entire mesh to the emission value defined."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object
            and context.active_object.type == 'MESH'
            and context.active_object.b2u_workflow in {'OPAQUE', 'OPAQUE_DS', 'TRANSPARENT', 'CRYSTAL'}
        )

    def execute(self, context):
        apply_to_object(context, 1, context.scene.b2u_settings.assigner_emission_val)
        self.report({'INFO'}, "Filled object with Emission!")
        return {'FINISHED'}


class B2U_OT_assign_alpha(bpy.types.Operator):
    """Sets the gradient/alpha mask (Vertex Color R) on the active selection"""
    bl_idname = "b2u.assign_alpha"
    bl_label = "Assign Alpha / Gradient"
    bl_description = "Sets the gradient mask (Opaques) or alpha mask (Transparents) of the selected vertices/edges/faces to the value defined."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == 'EDIT_MESH'
            and context.active_object
            and context.active_object.b2u_workflow in {'OPAQUE', 'OPAQUE_DS', 'TRANSPARENT', 'CRYSTAL'}
        )

    def execute(self, context):
        apply_to_selection(context, 0, context.scene.b2u_settings.assigner_alpha_val)
        self.report({'INFO'}, "Value applied to selection!")
        return {'FINISHED'}


class B2U_OT_fill_alpha(bpy.types.Operator):
    """Fills the gradient/alpha mask (Vertex Color R) across the entire mesh"""
    bl_idname = "b2u.fill_alpha"
    bl_label = "Fill Object Alpha / Gradient"
    bl_description = "Sets the gradient mask (Opaques) or alpha mask (Transparents) of the entire mesh to the value defined."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object
            and context.active_object.type == 'MESH'
            and context.active_object.b2u_workflow in {'OPAQUE', 'OPAQUE_DS', 'TRANSPARENT', 'CRYSTAL'}
        )

    def execute(self, context):
        apply_to_object(context, 0, context.scene.b2u_settings.assigner_alpha_val)
        self.report({'INFO'}, "Filled object with value!")
        return {'FINISHED'}


class B2U_OT_bake_thickness_alpha(bpy.types.Operator):
    """Bakes raycasted mesh thickness into Vertex Color R for depth-based transparency or gradients"""
    bl_idname = "b2u.bake_thickness_alpha"
    bl_label = "Bake Depth Alpha"
    bl_description = "Bake the alpha for transparents (Vertex R) using a projection through the mesh (thicker parts of the mesh will be more opaque, can be reversed)"
    bl_options = {'REGISTER', 'UNDO'}

    axis_mode: bpy.props.EnumProperty(
        name="Projection Axis",
        items=[
            ('NORMAL', "Inward Normals", ""),
            ('X', "X Axis", ""),
            ('Y', "Y Axis", ""),
            ('Z', "Z Axis", ""),
        ],
        default='NORMAL',
    )
    max_dist: bpy.props.FloatProperty(name="Max Depth Target", default=1.0, min=0.001)
    reverse: bpy.props.BoolProperty(name="Reverse Direction", default=False)

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        layer = ensure_vertex_color_layer(mesh, "B2U_VertexData")

        dg = context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(dg)
        bm = bmesh.new()
        bm.from_mesh(eval_obj.data)
        bm.transform(obj.matrix_world)

        bvh = mathutils.bvhtree.BVHTree.FromBMesh(bm)
        bm.free()

        mw = obj.matrix_world
        norm_mat = mw.to_3x3()

        # Precompute depth alpha once per unique vertex rather than once per face corner
        vert_alphas = [0.0] * len(mesh.vertices)
        for v in mesh.vertices:
            world_pos = mw @ v.co
            world_normal = (norm_mat @ v.normal).normalized()

            if self.axis_mode == 'NORMAL':
                ray_dir = -world_normal
            elif self.axis_mode == 'X':
                ray_dir = Vector((1.0, 0.0, 0.0))
                if world_normal.dot(ray_dir) > 0.0:
                    ray_dir = -ray_dir
            elif self.axis_mode == 'Y':
                ray_dir = Vector((0.0, 1.0, 0.0))
                if world_normal.dot(ray_dir) > 0.0:
                    ray_dir = -ray_dir
            elif self.axis_mode == 'Z':
                ray_dir = Vector((0.0, 0.0, 1.0))
                if world_normal.dot(ray_dir) > 0.0:
                    ray_dir = -ray_dir

            ray_origin = world_pos - (world_normal * 0.001)
            loc, _, _, dist = bvh.ray_cast(ray_origin, ray_dir)

            if not loc and self.axis_mode != 'NORMAL':
                loc, _, _, dist = bvh.ray_cast(ray_origin, -ray_dir)

            depth = dist if loc else 0.0
            alpha = min(depth / self.max_dist, 1.0)
            if self.reverse:
                alpha = 1.0 - alpha
            vert_alphas[v.index] = alpha

        for poly in mesh.polygons:
            for v_idx, l_idx in zip(poly.vertices, poly.loop_indices):
                c = layer.data[l_idx].color
                layer.data[l_idx].color = (vert_alphas[v_idx], c[1], c[2], c[3])

        mesh.update()
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "axis_mode")
        layout.prop(self, "max_dist")
        layout.prop(self, "reverse")

# endregion


# region Gradient Baking

class B2U_OT_bake_gradient(bpy.types.Operator):
    """Bakes a directional, depth, or curve-projected gradient into Vertex Color R"""
    bl_idname = "b2u.bake_gradient"
    bl_label = "Bake Gradient"
    bl_description = "Bake a gradient using the selected mode to the mesh."
    bl_options = {'REGISTER', 'UNDO'}

    axis: bpy.props.EnumProperty(
        name="Direction",
        items=[
            ('X', "X Axis", ""),
            ('Y', "Y Axis", ""),
            ('Z', "Z Axis", ""),
            ('CUSTOM AXIS', "Custom Axis", ""),
            ('DEPTH', "Depth (Inward)", ""),
            ('CURVE', "Curve", ""),
        ],
        default='Z',
    )
    bounds_min: bpy.props.FloatProperty(name="Start", default=-1.0)
    bounds_max: bpy.props.FloatProperty(name="End", default=1.0)
    max_dist: bpy.props.FloatProperty(name="Max Depth Target", default=1.0, min=0.001)
    reverse: bpy.props.BoolProperty(name="Reverse Direction", default=False)
    curve_name: bpy.props.StringProperty(name="Curve Name", default="")

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        mesh = obj.data
        settings = context.scene.b2u_settings
        layer = ensure_vertex_color_layer(mesh, "B2U_VertexData")

        vert_t = [0.0] * len(mesh.vertices)

        if self.axis == 'DEPTH':
            dg = context.evaluated_depsgraph_get()
            eval_obj = obj.evaluated_get(dg)
            bm = bmesh.new()
            bm.from_mesh(eval_obj.data)
            bm.transform(obj.matrix_world)

            bvh = mathutils.bvhtree.BVHTree.FromBMesh(bm)
            bm.free()

            mw = obj.matrix_world
            norm_mat = mw.to_3x3()

            for v in mesh.vertices:
                world_pos = mw @ v.co
                world_normal = (norm_mat @ v.normal).normalized()
                ray_dir = -world_normal
                ray_origin = world_pos - (world_normal * 0.001)

                loc, _, _, dist = bvh.ray_cast(ray_origin, ray_dir)
                if not loc:
                    loc, _, _, dist = bvh.ray_cast(ray_origin, -ray_dir)

                depth = dist if loc else 0.0
                t = min(depth / self.max_dist, 1.0)
                if self.reverse:
                    t = 1.0 - t
                vert_t[v.index] = t

        else:
            min_p = self.bounds_min
            max_p = self.bounds_max
            span = max_p - min_p
            if abs(span) < 0.0001:
                return {'CANCELLED'}

            if self.axis == 'CURVE':
                if not self.curve_name or self.curve_name not in bpy.data.objects:
                    self.report({'ERROR'}, "Please select a valid Curve Object!")
                    return {'CANCELLED'}

                curve_obj = bpy.data.objects[self.curve_name]
                dg = context.evaluated_depsgraph_get()
                curve_eval = curve_obj.evaluated_get(dg)

                try:
                    c_mesh = curve_eval.to_mesh()
                    c_mesh.transform(curve_obj.matrix_world)

                    pts = [v.co.copy() for v in c_mesh.vertices]
                    if len(pts) < 2:
                        return {'CANCELLED'}

                    segments = []
                    cum_len = [0.0]
                    for i in range(len(pts) - 1):
                        L = (pts[i + 1] - pts[i]).length
                        segments.append((pts[i], pts[i + 1], L))
                        cum_len.append(cum_len[-1] + L)
                    total_length = cum_len[-1] if cum_len[-1] > 0.0 else 0.0001

                    mw = obj.matrix_world
                    for v in mesh.vertices:
                        w_pos = mw @ v.co
                        min_dist = float('inf')
                        best_t = 0.0

                        for i, (p1, p2, L) in enumerate(segments):
                            if L == 0.0:
                                continue
                            dir_vec = (p2 - p1) / L
                            proj_t = (w_pos - p1).dot(dir_vec)
                            proj_t_clamped = max(0.0, min(L, proj_t))
                            closest_pt = p1 + dir_vec * proj_t_clamped

                            dist = (w_pos - closest_pt).length
                            if dist < min_dist:
                                min_dist = dist
                                best_t = (cum_len[i] + proj_t_clamped) / total_length

                        t = max(0.0, min(1.0, (best_t - min_p) / span))
                        if self.reverse:
                            t = 1.0 - t
                        vert_t[v.index] = t
                finally:
                    curve_eval.to_mesh_clear()

            else:
                if self.axis == 'X':
                    vec = Vector((1.0, 0.0, 0.0))
                elif self.axis == 'Y':
                    vec = Vector((0.0, 1.0, 0.0))
                elif self.axis == 'Z':
                    vec = Vector((0.0, 0.0, 1.0))
                elif self.axis == 'CUSTOM AXIS':
                    vec = Vector(settings.grad_custom_end) - Vector(settings.grad_custom_start)

                if vec.length < 0.0001:
                    return {'CANCELLED'}
                vec.normalize()

                for v in mesh.vertices:
                    proj = v.co.dot(vec)
                    t = max(0.0, min(1.0, (proj - min_p) / span))
                    if self.reverse:
                        t = 1.0 - t
                    vert_t[v.index] = t

        for poly in mesh.polygons:
            for v_idx, l_idx in zip(poly.vertices, poly.loop_indices):
                c = layer.data[l_idx].color
                layer.data[l_idx].color = (vert_t[v_idx], c[1], c[2], c[3])

        mesh.update()
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "axis")

        if self.axis == 'CUSTOM AXIS':
            layout.label(text="Uses Custom Vector from N-Panel", icon='INFO')
        elif self.axis == 'CURVE':
            layout.label(text=f"Target: {self.curve_name}", icon='CURVE_DATA')

        row = layout.row(align=True)
        if self.axis == 'CURVE':
            row.prop(self, "bounds_min", text="Start Eval")
            row.prop(self, "bounds_max", text="End Eval")
        elif self.axis == 'DEPTH':
            row.prop(self, "max_dist", text="Max Depth")
        else:
            row.prop(self, "bounds_min", text="Start")
            row.prop(self, "bounds_max", text="End")

        layout.prop(self, "reverse")


class B2U_OT_clear_gradient(bpy.types.Operator):
    """Resets Vertex Color R to neutral (0.5) across the entire mesh"""
    bl_idname = "b2u.clear_gradient"
    bl_label = "Clear Gradient"
    bl_description = "Resets the gradient mask (Vertex R) to 0.5 for the entire mesh"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object
            and context.active_object.type == 'MESH'
            and context.active_object.b2u_workflow in {'OPAQUE', 'OPAQUE_DS', 'CRYSTAL'}
        )

    def execute(self, context):
        apply_to_object(context, 0, 0.5)
        self.report({'INFO'}, "Cleared object Gradient to neutral (0.5)!")
        return {'FINISHED'}


class B2U_OT_set_gradient_vector(bpy.types.Operator):
    """Acquires a custom gradient axis vector from two selected vertices in Edit Mode"""
    bl_idname = "b2u.set_gradient_vector"
    bl_label = "Set Custom Vector"
    bl_description = "Select two vertices to define a custom gradient direction vector."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        bm = bmesh.from_edit_mesh(context.active_object.data)
        sel = [v for v in bm.verts if v.select]
        if len(sel) != 2:
            self.report({'ERROR'}, "Select exactly 2 vertices to define a vector!")
            return {'CANCELLED'}
        settings = context.scene.b2u_settings
        settings.grad_custom_start = sel[0].co
        settings.grad_custom_end = sel[1].co
        settings.grad_show_custom = True
        return {'FINISHED'}

# endregion


# region Fluid Flow

class B2U_OT_add_live_flow_modifier(bpy.types.Operator):
    """Computes tangent basis attributes and attaches the B2U_LiveFlow_Engine Geometry Nodes modifier"""
    bl_idname = "b2u.add_live_flow_modifier"
    bl_label = "Add Live Flow Engine"
    bl_description = "Initializes the mesh tangents and attaches the real-time Geometry Nodes flow engine"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.b2u_settings

        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "A mesh must be selected")
            return {'CANCELLED'}

        old_mode = obj.mode
        if old_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        mesh = obj.data
        is_liquid = getattr(obj, 'b2u_workflow', 'NONE') == 'LIQUID'

        if not mesh.uv_layers.active:
            self.report({'ERROR'}, "Mesh requires an Active UV Map to calculate flow!")
            if old_mode != 'OBJECT':
                bpy.ops.object.mode_set(mode=old_mode)
            return {'CANCELLED'}

        gn_name = "B2U_LiveFlow_Engine"
        mod = obj.modifiers.get(gn_name)
        is_new_mod = (mod is None)

        existing_props = {}
        if not is_new_mod and mod.node_group:
            def get_id(name):
                if hasattr(mod.node_group, "interface"):
                    for i in mod.node_group.interface.items_tree:
                        if i.item_type == 'SOCKET' and i.name == name:
                            return i.identifier
                else:
                    for i in mod.node_group.inputs:
                        if i.name == name:
                            return i.identifier
                return None

            prop_names = (
                "Flow Curve", "Flow Strength", "Use Curve Falloff",
                "Curve Falloff Dist", "Collisions", "Collision Influence",
                "Pressure Factor", "Surface Hover Threshold", "Flow Smoothing",
            )
            for key in prop_names:
                sid = get_id(key)
                if sid and sid in mod:
                    existing_props[key] = mod[sid]

        if is_new_mod:
            curve_obj = settings.flow_target_curve
            if not curve_obj or curve_obj.type != 'CURVE':
                self.report({'ERROR'}, "Please select a valid Target Curve in the Global UI!")
                if old_mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode=old_mode)
                return {'CANCELLED'}

        compute_flow_tangent_basis(mesh, is_liquid)

        group = ensure_gn_live_flow()

        if is_new_mod:
            mod = obj.modifiers.new(name=gn_name, type='NODES')
            mod.node_group = group
        elif mod.node_group != group:
            mod.node_group = group

        def map_and_set(prop_name, default_val):
            sid = None
            if hasattr(group, "interface"):
                for i in group.interface.items_tree:
                    if i.item_type == 'SOCKET' and i.name == prop_name:
                        sid = i.identifier
            else:
                for i in group.inputs:
                    if i.name == prop_name:
                        sid = i.identifier

            if sid:
                if not is_new_mod and prop_name in existing_props:
                    mod[sid] = existing_props[prop_name]
                else:
                    mod[sid] = default_val

        map_and_set("Flow Curve", settings.flow_target_curve)
        map_and_set("Flow Strength", 1.0)
        map_and_set("Use Curve Falloff", False)
        map_and_set("Curve Falloff Dist", 10.0)
        map_and_set("Collisions", settings.flow_collision_collection)
        map_and_set("Collision Influence", 5.0)
        map_and_set("Pressure Factor", 0.5)
        map_and_set("Surface Hover Threshold", 0.1)
        map_and_set("Flow Smoothing", 6)

        if old_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=old_mode)

        self.report({'INFO'}, "Live Flow Engine Attached!")
        return {'FINISHED'}


class B2U_OT_flow_reset(bpy.types.Operator):
    """Removes the live flow modifier and resets flow attributes to neutral"""
    bl_idname = "b2u.flow_reset"
    bl_label = "Reset Flow Map"
    bl_description = "Removes the live flow engine and resets the flow map to neutral"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return {'CANCELLED'}

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        mod = obj.modifiers.get("B2U_LiveFlow_Engine")
        if mod:
            obj.modifiers.remove(mod)

        if obj.b2u_workflow == 'LIQUID':
            flow_uv = obj.data.uv_layers.get("B2U_LiquidFlow")
            if flow_uv:
                for d in flow_uv.data:
                    d.uv = (0.0, 0.0)
            obj.data.update()
            return {'FINISHED'}

        layer = obj.data.color_attributes.get("B2U_VertexData")
        if not layer:
            return {'CANCELLED'}
        b_val = 1.0 if context.scene.b2u_settings.invert_b else 0.0
        for d in layer.data:
            d.color = (0.5, 0.5, b_val, d.color[3])
        obj.data.update()
        return {'FINISHED'}


class B2U_OT_reverse_curve(bpy.types.Operator):
    """Flips the direction of all splines on the active curve object"""
    bl_idname = "b2u.reverse_curve"
    bl_label = "Reverse Flow Direction"
    bl_description = "Flips the structural direction of the selected flow curves"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'CURVE'

    def execute(self, context):
        original_mode = context.active_object.mode
        try:
            if original_mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.curve.select_all(action='SELECT')
            bpy.ops.curve.switch_direction()
        finally:
            if context.active_object.mode != original_mode:
                bpy.ops.object.mode_set(mode=original_mode)
        return {'FINISHED'}

# endregion


# region GPU Vector Drawing

def manage_flow_handler(state):
    """Registers or removes the 3D Viewport draw handler for fluid flow vectors."""
    global flow_draw_handle
    if state:
        if flow_draw_handle is None:
            flow_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                draw_flow_vectors, (), 'WINDOW', 'POST_VIEW'
            )
    else:
        if flow_draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(flow_draw_handle, 'WINDOW')
            flow_draw_handle = None


def manage_grad_handler(state):
    """Registers or removes the 3D Viewport draw handler for custom gradient and grain vectors."""
    global grad_draw_handle
    if state:
        if grad_draw_handle is None:
            def combined_draw():
                draw_custom_gradient_vector()
                draw_grain_vectors()

            grad_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
                combined_draw, (), 'WINDOW', 'POST_VIEW'
            )
    else:
        if grad_draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(grad_draw_handle, 'WINDOW')
            grad_draw_handle = None


def draw_custom_gradient_vector():
    """Draws the start-to-end custom direction arrow in the 3D Viewport."""
    context = bpy.context
    settings = getattr(context.scene, 'b2u_settings', None)
    if not settings or not settings.grad_show_custom:
        return

    obj = context.active_object
    if not obj or obj.type != 'MESH':
        return

    mw = obj.matrix_world
    start = mw @ Vector(settings.grad_custom_start)
    end = mw @ Vector(settings.grad_custom_end)
    vec = end - start

    if vec.length < 0.001:
        return

    lines = [start, end]
    colors = [(0.0, 1.0, 0.0, 1.0), (1.0, 0.0, 0.0, 1.0)]

    arrow_size = min(0.5, vec.length * 0.2)
    norm = vec.normalized()
    mat = norm.to_track_quat('Z', 'Y').to_matrix().to_4x4()

    for angle in (math.radians(150), math.radians(210)):
        w = Vector((0.0, math.sin(angle) * arrow_size, math.cos(angle) * arrow_size))
        lines.extend([end, end + (mat @ w)])
        colors.extend([(1.0, 0.0, 0.0, 1.0), (1.0, 0.0, 0.0, 1.0)])

    shader = gpu.shader.from_builtin(SHADER_3D_SMOOTH_COLOR)
    batch = batch_for_shader(shader, 'LINES', {"pos": lines, "color": colors})

    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(3.0)
    shader.bind()
    batch.draw(shader)
    gpu.state.blend_set('NONE')
    gpu.state.depth_test_set('LESS_EQUAL')


def draw_flow_vectors():
    """Draws per-vertex tangent-space flow arrows on the active mesh in the 3D Viewport."""
    try:
        context = bpy.context
        scene = context.scene
        settings = getattr(scene, 'b2u_settings', None)
        if not settings or not settings.flow_show_vectors:
            return
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return

        dg = context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(dg)
        eval_mesh = eval_obj.to_mesh(preserve_all_data_layers=True, depsgraph=dg)

        try:
            is_liquid = getattr(obj, 'b2u_workflow', 'NONE') == 'LIQUID'

            if is_liquid:
                flow_layer = eval_mesh.uv_layers.get("B2U_LiquidFlow")
                data_layer = eval_mesh.uv_layers.get("B2U_LiquidData")
                if not flow_layer or not data_layer:
                    return
            else:
                data_layer = eval_mesh.color_attributes.get("B2U_VertexData") or eval_mesh.attributes.get("B2U_VertexData")
                if not data_layer:
                    return

            attr_tan = eval_mesh.attributes.get("B2U_Tangent")
            attr_bitan = eval_mesh.attributes.get("B2U_Bitangent")
            if not attr_tan or not attr_bitan:
                return

            loop_indices = [0] * len(eval_mesh.vertices)
            for p in eval_mesh.polygons:
                for v_idx, l_idx in zip(p.vertices, p.loop_indices):
                    loop_indices[v_idx] = l_idx

            mw = obj.matrix_world
            mw_3x3 = mw.to_3x3()
            lines, colors = [], []
            scale, user_color = settings.flow_vector_length, settings.flow_vector_color

            try:
                view_mat = context.region_data.view_matrix.inverted()
                cam_dir = (mw.translation - view_mat.translation).normalized()
            except AttributeError:
                cam_dir = Vector((0.0, 0.0, 1.0))

            def get_vec(item):
                return Vector(item.vector if hasattr(item, 'vector') else item.color)

            def get_val(item):
                if hasattr(item, 'uv'):
                    return item.uv
                if hasattr(item, 'color'):
                    return item.color
                if hasattr(item, 'vector'):
                    return item.vector
                return (0.0, 0.0, 0.0, 0.0)

            for vert in eval_mesh.vertices:
                l_idx = loop_indices[vert.index]

                if is_liquid:
                    f_uv = get_val(flow_layer.data[l_idx])
                    d_uv = get_val(data_layer.data[l_idx])

                    dir_x = -f_uv[0] if settings.invert_r else f_uv[0]
                    dir_y = -f_uv[1] if settings.invert_g else f_uv[1]

                    strength = min(d_uv[0] * 2.0, 1.0)
                    if settings.invert_b:
                        strength = 1.0 - strength
                else:
                    c = get_val(data_layer.data[l_idx])
                    strength = 1.0 - c[2] if settings.invert_b else c[2]
                    dir_x = ((1.0 - c[0]) * 2.0) - 1.0 if settings.invert_r else (c[0] * 2.0) - 1.0
                    dir_y = ((1.0 - c[1]) * 2.0) - 1.0 if settings.invert_g else (c[1] * 2.0) - 1.0

                if strength < 0.05:
                    continue

                tan = get_vec(attr_tan.data[l_idx])
                bitan = get_vec(attr_bitan.data[l_idx])

                world_tan = (mw_3x3 @ tan).normalized()
                world_bitan = (mw_3x3 @ bitan).normalized()

                true_3d_flow = (world_tan * dir_x) + (world_bitan * dir_y)
                flow_magnitude = true_3d_flow.length

                if flow_magnitude > 0.0:
                    true_3d_flow.normalize()
                else:
                    continue

                start_pos = mw @ vert.co
                current_scale = scale * strength * (flow_magnitude if is_liquid else 1.0)
                end_pos = start_pos + (true_3d_flow * current_scale)

                lines.extend([start_pos, end_pos])

                l_color = (user_color[0] * strength, user_color[1] * strength, user_color[2] * strength, 1.0)
                colors.extend([l_color, l_color])

                right = true_3d_flow.cross(cam_dir)
                if right.length < 0.001:
                    right = true_3d_flow.cross(Vector((0.0, 1.0, 0.0)))
                    if right.length < 0.001:
                        right = true_3d_flow.cross(Vector((1.0, 0.0, 0.0)))
                right.normalize()

                arrow_size = 0.25 * current_scale
                wing1 = (-true_3d_flow * 0.866 + right * 0.5) * arrow_size
                wing2 = (-true_3d_flow * 0.866 - right * 0.5) * arrow_size

                lines.extend([end_pos, end_pos + wing1, end_pos, end_pos + wing2])
                colors.extend([l_color, l_color, l_color, l_color])
        finally:
            eval_obj.to_mesh_clear()

        if not lines:
            return

        shader = gpu.shader.from_builtin(SHADER_3D_SMOOTH_COLOR)
        batch = batch_for_shader(shader, 'LINES', {"pos": lines, "color": colors})

        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.0)
        shader.bind()
        batch.draw(shader)
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('LESS_EQUAL')

    except Exception as e:
        print(f"B2U Vector Draw Error: {e}")


def draw_grain_vectors():
    """Draws per-face spherical grain direction lines using the B2U_Preview_Grain attribute."""
    context = bpy.context
    settings = getattr(context.scene, 'b2u_settings', None)

    if not settings or not settings.grad_show_custom:
        return

    obj = context.active_object
    if not obj or obj.type != 'MESH':
        return

    mesh = obj.data
    mw = obj.matrix_world
    mw_3x3 = mw.to_3x3()
    lines = []
    colors = []

    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(mesh)
        grain_layer = bm.loops.layers.float_vector.get("B2U_Preview_Grain")
        if not grain_layer:
            return

        for face in bm.faces:
            if not face.loops:
                continue
            uv = face.loops[0][grain_layer]

            theta = (uv[0] - 0.5) * (2.0 * math.pi)
            phi = (uv[1] - 0.5) * math.pi

            local_dir = Vector((
                math.cos(phi) * math.cos(theta),
                math.cos(phi) * math.sin(theta),
                math.sin(phi),
            ))
            if local_dir.length < 0.01:
                continue
            world_dir = (mw_3x3 @ local_dir).normalized()

            center = mw @ face.calc_center_median()
            lines.extend([center, center + (world_dir * 0.25)])
            colors.extend([(0.0, 1.0, 1.0, 1.0), (0.0, 1.0, 1.0, 1.0)])
    else:
        grain_attr = mesh.attributes.get("B2U_Preview_Grain")
        if not grain_attr or len(grain_attr.data) == 0:
            return
        attr_data = grain_attr.data

        for poly in mesh.polygons:
            if not poly.loop_indices:
                continue
            loop_idx = poly.loop_indices[0]
            if loop_idx >= len(attr_data):
                continue

            uv = attr_data[loop_idx].vector

            theta = (uv[0] - 0.5) * (2.0 * math.pi)
            phi = (uv[1] - 0.5) * math.pi

            local_dir = Vector((
                math.cos(phi) * math.cos(theta),
                math.cos(phi) * math.sin(theta),
                math.sin(phi),
            ))
            if local_dir.length < 0.01:
                continue
            world_dir = (mw_3x3 @ local_dir).normalized()

            center = mw @ poly.center
            lines.extend([center, center + (world_dir * 0.25)])
            colors.extend([(0.0, 1.0, 1.0, 1.0), (0.0, 1.0, 1.0, 1.0)])

    if not lines:
        return

    shader = gpu.shader.from_builtin(SHADER_3D_SMOOTH_COLOR)
    batch = batch_for_shader(shader, 'LINES', {"pos": lines, "color": colors})

    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)
    shader.bind()
    batch.draw(shader)
    gpu.state.blend_set('NONE')

# endregion


@bpy.app.handlers.persistent
def b2u_mode_tracker_handler(scene, depsgraph=None):
    """Recomputes flow tangents and optional edge-wear distance fields when exiting Edit Mode."""
    global last_mode
    try:
        current_mode = bpy.context.mode
        if last_mode == 'EDIT_MESH' and current_mode == 'OBJECT':
            obj = bpy.context.active_object
            if obj and obj.type == 'MESH':
                settings = bpy.context.scene.b2u_settings

                if obj.modifiers.get("B2U_LiveFlow_Engine"):
                    is_liquid = getattr(obj, 'b2u_workflow', 'NONE') == 'LIQUID'
                    compute_flow_tangent_basis(obj.data, is_liquid)

                if settings.auto_bake_edgewear and obj.b2u_workflow in {'OPAQUE', 'OPAQUE_DS', 'TRANSPARENT'}:
                    def trigger_auto_bake():
                        try:
                            bpy.ops.b2u.bake_physical_distance('EXEC_DEFAULT')
                        except RuntimeError:
                            pass
                        return None

                    bpy.app.timers.register(trigger_auto_bake, first_interval=0.01)

        last_mode = current_mode
    except Exception:
        pass


# region Registration

classes = (
    B2U_OT_assign_material_id,
    B2U_OT_fill_material_id,
    B2U_OT_bake_grain_uvs,
    B2U_OT_bake_automatic_grain,
    B2U_OT_set_grain_vector,
    B2U_OT_bake_physical_distance,
    B2U_OT_clear_physical_distance,
    B2U_OT_apply_edge_thickness,
    B2U_OT_assign_paint_mask,
    B2U_OT_fill_paint_mask,
    B2U_OT_assign_emission,
    B2U_OT_fill_emission,
    B2U_OT_assign_alpha,
    B2U_OT_fill_alpha,
    B2U_OT_bake_thickness_alpha,
    B2U_OT_bake_gradient,
    B2U_OT_clear_gradient,
    B2U_OT_set_gradient_vector,
    B2U_OT_add_live_flow_modifier,
    B2U_OT_flow_reset,
    B2U_OT_reverse_curve,
)

addon_keymaps = []


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.get('Mesh')
        if not km:
            km = kc.keymaps.new(name='Mesh', space_type='EMPTY')
        kmi = km.keymap_items.new(B2U_OT_assign_material_id.bl_idname, 'A', 'PRESS', ctrl=True)
        addon_keymaps.append((km, kmi))

    if b2u_mode_tracker_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(b2u_mode_tracker_handler)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        for km, kmi in addon_keymaps:
            km.keymap_items.remove(kmi)
    addon_keymaps.clear()

    manage_flow_handler(False)
    manage_grad_handler(False)

    if b2u_mode_tracker_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(b2u_mode_tracker_handler)

# endregion