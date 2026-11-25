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

import os
import textwrap
from collections import defaultdict
import bpy


# region Helper Functions

def get_gn_socket_map(node_group):
    """Returns a dictionary mapping socket names to their identifiers for a Geometry Nodes group."""
    socket_map = {}
    if not node_group:
        return socket_map

    if hasattr(node_group, "interface"):
        for item in node_group.interface.items_tree:
            if item.item_type == 'SOCKET':
                socket_map[item.name] = item.identifier
    else:
        for inp in node_group.inputs:
            socket_map[inp.name] = inp.identifier
    return socket_map


def _safe_enum_int(block):
    """Safely converts a B2UDataBlock's e_val string to an integer index."""
    if not block:
        return 0
    try:
        return int(block.e_val)
    except (ValueError, TypeError):
        return 0


def draw_material_schema_properties(layout, scene, settings, mat, mat_index):
    """Draws the dynamic schema property blocks for a material, grouped by LUT pixel."""
    box = layout.box()
    box.label(text="Material Data (Grouped by LUT Pixel):", icon='PROPERTIES')

    schema_lookup = 'OPAQUE' if mat.workflow == 'OPAQUE_DS' else mat.workflow
    active_defs = [s for s in settings.slot_defs if s.schema == schema_lookup and s.is_active]

    pixel_groups = defaultdict(list)
    for s_def in active_defs:
        parts = s_def.slot_id.split('_')
        g_key = f"{parts[0]}_{parts[1]}" if len(parts) >= 3 else f"{parts[0]}_alpha"
        pixel_groups[g_key].append(s_def)

    det_block = mat.data_blocks.get("r1_p4_g")
    e_block = mat.data_blocks.get("r1_p4_b")
    i_block = mat.data_blocks.get("r1_p4_a")

    is_detail_mapped = _safe_enum_int(det_block) > 0
    is_emissive_or_iridescent = (_safe_enum_int(e_block) > 0) or (_safe_enum_int(i_block) > 0)

    for g_key, defs in pixel_groups.items():
        renderable_defs = []
        for s_def in defs:
            if settings.palette_uses_default_schema:
                id_values = s_def.slot_id.split('_')
                skip = False

                if mat.workflow == 'OPAQUE':
                    if id_values[1] == "p1" and not is_emissive_or_iridescent:
                        skip = True
                    elif id_values[1] == "p2" and not is_detail_mapped:
                        skip = True
                    elif id_values[1] == "p3":
                        skip = True
                elif mat.workflow in {'OPAQUE_DS', 'TRANSPARENT'}:
                    if id_values[1] == "p1" and not is_emissive_or_iridescent:
                        skip = True
                    elif id_values[1] == "p2" and not is_detail_mapped:
                        skip = True
                elif mat.workflow == 'LIQUID':
                    if id_values[0] == "r1" and id_values[1] == "p3" and not is_emissive_or_iridescent:
                        skip = True

                if skip:
                    continue

            block = mat.data_blocks.get(s_def.slot_id)
            if block:
                renderable_defs.append((s_def, block))

        if not renderable_defs:
            continue

        row = box.row()
        for s_def, block in renderable_defs:
            col = row.column(align=True)
            col.label(text=s_def.ui_name)

            if s_def.data_type == 'FLOAT':
                col.prop(block, "f_val", text=f"[{round(s_def.min_val, 2)} to {round(s_def.max_val, 2)}]")
            elif s_def.data_type == 'INT':
                col.prop(block, "i_val", text=f"[{int(s_def.min_val)} to {int(s_def.max_val)}]")
            elif s_def.data_type == 'BOOL':
                col.prop(block, "b_val", text="Enabled")
            elif s_def.data_type == 'ENUM':
                col.prop(block, "e_val", text="")
            elif s_def.data_type == 'BITMASK':
                op = col.operator("b2u.edit_bitmask", text="Edit Flags", icon='PREFERENCES')
                op.mat_index = mat_index
                op.slot_id = s_def.slot_id

            if s_def.ui_description:
                desc_col = col.column(align=True)
                desc_col.active = False
                for i, line in enumerate(textwrap.wrap(s_def.ui_description, width=60)):
                    desc_col.label(text=line, icon='INFO' if i == 0 else 'BLANK1')

        for _ in range(4 - len(renderable_defs)):
            row.column()
        box.separator(factor=0.5)


def draw_material_color_properties(layout, settings, mat):
    """Draws the variant and dynamic biome color pickers for a material."""
    box = layout.box()
    box.label(text="Material Colors:", icon='COLOR')

    if mat.workflow in {'OPAQUE', 'OPAQUE_DS', 'TRANSPARENT'}:
        if mat.biome_type in {'INDEPENDENT', 'INDEP_DYN'}:
            box.label(text="Color Variants (1-8):", icon='GROUP_VCOL')
            box.prop(settings, "assigner_variant", text="Edit Variant")
            idx = int(settings.assigner_variant)
            if 0 <= idx < len(mat.variant_colors):
                v_data = mat.variant_colors[idx]
                v_row = box.row(align=True)
                v_row.prop(v_data, "base_0", text=f"Variant {idx + 1} Base")
                v_row.prop(v_data, "emission", text="Emission")

        if mat.biome_type in {'DYNAMIC', 'INDEP_DYN'}:
            if mat.biome_type == 'INDEP_DYN':
                box.separator()
            box.label(text="Dynamic Biome Colors:", icon='GROUP_VCOL')
            box.prop(settings, "preview_biome", text="Edit Biome")
            idx = int(settings.preview_biome)
            if 0 <= idx < len(mat.dynamic_colors):
                b_data = mat.dynamic_colors[idx]
                c_row = box.row()
                for i in range(settings.num_subtypes):
                    s_col = c_row.column(align=True)
                    s_col.label(text=settings.subtype_names[i].name)
                    s_col.prop(b_data, f"base_{i}", text="")
                e_col = c_row.column(align=True)
                e_col.label(text="Emission")
                e_col.prop(b_data, "emission", text="")

    elif mat.workflow == 'CRYSTAL':
        if mat.biome_type in {'INDEPENDENT', 'INDEP_DYN'} and len(mat.variant_colors) > 0:
            box.label(text="Global Crystal Colors:", icon='COLOR')
            v_row = box.row(align=True)
            v_row.prop(mat.variant_colors[0], "base_0", text="Base Color")
            v_row.prop(mat.variant_colors[0], "emission", text="Emission Color")

        if mat.biome_type in {'DYNAMIC', 'INDEP_DYN'}:
            if mat.biome_type == 'INDEP_DYN':
                box.separator()
            box.label(text="Dynamic Biome Colors:", icon='GROUP_VCOL')
            box.prop(settings, "preview_biome", text="Edit Biome")
            idx = int(settings.preview_biome)
            if 0 <= idx < len(mat.dynamic_colors):
                b_data = mat.dynamic_colors[idx]
                c_row = box.row()
                for i in range(settings.num_subtypes):
                    s_col = c_row.column(align=True)
                    s_col.label(text=settings.subtype_names[i].name)
                    s_col.prop(b_data, f"base_{i}", text="")
                e_col = c_row.column(align=True)
                e_col.label(text="Emission")
                e_col.prop(b_data, "emission", text="")

    elif mat.workflow == 'LIQUID':
        if mat.biome_type in {'INDEPENDENT', 'INDEP_DYN'} and len(mat.variant_colors) > 0:
            box.label(text="Global Liquid Colors:", icon='MATFLUID')
            v_data = mat.variant_colors[0]
            r1_row = box.row(align=True)
            r1_row.prop(v_data, "base_0", text="Surface")
            r1_row.prop(v_data, "emission", text="Pri. Emission")
            r2_row = box.row(align=True)
            r2_row.prop(v_data, "depth_0", text="Depth")
            r2_row.prop(v_data, "sec_emission", text="Sec. Emission")

        if mat.biome_type in {'DYNAMIC', 'INDEP_DYN'}:
            if mat.biome_type == 'INDEP_DYN':
                box.separator()
            box.label(text="Dynamic Biome Colors:", icon='GROUP_VCOL')
            box.prop(settings, "preview_biome", text="Edit Biome")
            idx = int(settings.preview_biome)
            if 0 <= idx < len(mat.dynamic_colors):
                b_data = mat.dynamic_colors[idx]
                box.label(text="Row 1: Surface Dynamics", icon='MATFLUID')
                r1_row = box.row()
                for i in range(settings.num_subtypes):
                    s_col = r1_row.column(align=True)
                    s_col.label(text=settings.subtype_names[i].name)
                    s_col.prop(b_data, f"base_{i}", text="")
                e1_col = r1_row.column(align=True)
                e1_col.label(text="Primary Emission")
                e1_col.prop(b_data, "emission", text="")

                box.separator()
                box.label(text="Row 2: Depth Dynamics", icon='MATFLUID')
                r2_row = box.row()
                for i in range(settings.num_subtypes):
                    s_col = r2_row.column(align=True)
                    s_col.label(text=settings.subtype_names[i].name)
                    s_col.prop(b_data, f"depth_{i}", text="")
                e2_col = r2_row.column(align=True)
                e2_col.label(text="Secondary Emission")
                e2_col.prop(b_data, "sec_emission", text="")

# endregion


# region Operators & Panels

class B2U_OT_force_refresh_data(bpy.types.Operator):
    """Purges and re-imports preview shaders, reloads textures from disk, and rebinds all scene objects"""
    bl_idname = "b2u.force_refresh_data"
    bl_label = "Force Refresh Data & Shaders"
    bl_description = "Purges existing preview materials, re-imports them from the addon directory, reloads textures from disk, and rebinds everything"

    def execute(self, context):
        settings = context.scene.b2u_settings

        from .data_properties import (
            B2U_UNIQUE_PREVIEW_MATS,
            get_master_mat_map,
            get_or_load_preview_material,
            push_schema_bounds_to_shaders,
            push_shader_uniforms,
            update_shader_preview_nodes,
        )
        from .ops_meshtools import build_blender_preview_lut

        for mat_name in B2U_UNIQUE_PREVIEW_MATS:
            mat = bpy.data.materials.get(mat_name)
            if mat:
                bpy.data.materials.remove(mat, do_unlink=True)

        for group in list(bpy.data.node_groups):
            if isinstance(group, bpy.types.ShaderNodeTree) and group.name.startswith("B2U_"):
                bpy.data.node_groups.remove(group, do_unlink=True)

        if settings.detail_atlas_image:
            settings.detail_atlas_image.reload()
        if settings.emission_pattern_image:
            settings.emission_pattern_image.reload()
        if settings.iridescence_lut_image:
            settings.iridescence_lut_image.reload()

        mat_name_map = get_master_mat_map()
        for obj in context.scene.objects:
            if obj.type == 'MESH':
                wf = getattr(obj, 'b2u_workflow', 'NONE')
                if wf in mat_name_map:
                    preview_mat = get_or_load_preview_material(mat_name_map[wf])
                    if preview_mat:
                        obj.data.materials.clear()
                        obj.data.materials.append(preview_mat)

        try:
            build_blender_preview_lut()
            update_shader_preview_nodes(None, context)
            push_schema_bounds_to_shaders(context)
            push_shader_uniforms(settings, context)
        except Exception as e:
            print(f"B2U Refresh Warning: Could not push shader uniforms - {e}")

        if context.view_layer:
            context.view_layer.update()
        self.report({'INFO'}, "Successfully purged, reloaded, and rebound all B2U data!")
        return {'FINISHED'}


class B2U_OT_clear_palette_data(bpy.types.Operator):
    """Clears all material slots from the current scene"""
    bl_idname = "b2u.clear_palette_data"
    bl_label = "Clear Orphaned .blend Data"
    bl_description = "Deletes all ghost material data permanently stored in this .blend file (ONLY USE TO FORCE A RELOAD FROM JSON PALETTE)"

    def execute(self, context):
        context.scene.b2u_materials.clear()
        context.scene.b2u_settings.is_dirty = False
        self.report({'INFO'}, "Cleared orphaned B2U Material Data")
        return {'FINISHED'}


def on_palette_dir_update(self, context):
    """Reloads the active JSON palette when the palette folder path is updated in Preferences."""
    try:
        if context.scene.b2u_settings.active_palette_file != "NONE":
            bpy.app.timers.register(lambda: bpy.ops.b2u.load_schema_json() and None, first_interval=0.1)
    except Exception:
        pass


class B2U_Preferences(bpy.types.AddonPreferences):
    """Global add-on preferences for Unity LUT export and JSON palette directory paths."""
    bl_idname = __package__ or "b2u_material_master"

    unity_export_path: bpy.props.StringProperty(name="Unity Export Path", subtype='DIR_PATH', default="")
    json_palette_dir: bpy.props.StringProperty(
        name="Palette Folder",
        description="Folder where all your .json palette files are stored",
        subtype='DIR_PATH',
        default="",
        update=on_palette_dir_update,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "unity_export_path")
        layout.prop(self, "json_palette_dir")


class B2U_OT_setup_workspace(bpy.types.Operator):
    """Creates or switches to a dedicated B2U Palette workspace tab"""
    bl_idname = "b2u.setup_workspace"
    bl_label = "Create B2U Workspace"
    bl_description = "Creates a dedicated workspace tab for the Palette Manager"

    def execute(self, context):
        if "B2U Palette" in bpy.data.workspaces:
            context.window.workspace = bpy.data.workspaces["B2U Palette"]
            self.report({'INFO'}, "Switched to B2U Workspace!")
            return {'FINISHED'}

        old_workspaces = {ws.name for ws in bpy.data.workspaces}
        bpy.ops.workspace.duplicate()

        new_ws = next((ws for ws in bpy.data.workspaces if ws.name not in old_workspaces), None)
        if new_ws:
            new_ws.name = "B2U Palette"
            context.window.workspace = new_ws

        def setup_ui():
            if bpy.context.window and bpy.context.window.screen:
                max_area = max(bpy.context.window.screen.areas, key=lambda a: a.width * a.height)
                if max_area.type != 'PROPERTIES':
                    max_area.type = 'PROPERTIES'
            return None

        bpy.app.timers.register(setup_ui, first_interval=0.05)
        self.report({'INFO'}, "Workspace Generated! The palette settings are under the scene tab.")
        return {'FINISHED'}


class B2U_UL_material_list(bpy.types.UIList):
    """UIList displaying palette materials sorted by their starting LUT row index."""
    WF_NAMES = {
        'OPAQUE': "Opaque",
        'OPAQUE_DS': "Opaque Double Sided",
        'TRANSPARENT': "Transparent",
        'LIQUID': "Liquid",
        'CRYSTAL': "Crystal",
    }
    BT_NAMES = {
        'INDEPENDENT': "Independent",
        'DYNAMIC': "Dynamic",
        'INDEP_DYN': "Independent & Dynamic",
    }

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if item.workflow == 'LIQUID':
            custom_icon = 'MATFLUID'
        elif item.workflow in {'TRANSPARENT', 'CRYSTAL'}:
            custom_icon = 'NODE_MATERIAL'
        else:
            custom_icon = 'MATERIAL'

        split_main = layout.split(factor=0.15)
        id_text = (
            f"ID: {item.start_row_index}-{item.start_row_index + 1}"
            if item.workflow == "LIQUID" or item.biome_type == "INDEP_DYN"
            else f"ID: {item.start_row_index}"
        )
        split_main.column().label(text=id_text, icon='LOCKED' if item.is_locked else 'NONE')

        split_rest = split_main.column().split(factor=0.33)
        split_rest.column().label(text=item.mat_name, icon=custom_icon)

        split_tail = split_rest.column().split(factor=0.5)
        split_tail.column().label(text=self.WF_NAMES.get(item.workflow, item.workflow))
        split_tail.column().label(text=self.BT_NAMES.get(item.biome_type, item.biome_type))

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt_flags = [self.bitflag_filter_item] * len(items)
        ordered_indices = [idx for idx, _ in sorted(enumerate(items), key=lambda x: x[1].start_row_index)]
        flt_neworder = [0] * len(items)
        for new_pos, orig_idx in enumerate(ordered_indices):
            flt_neworder[orig_idx] = new_pos
        return flt_flags, flt_neworder


class B2U_OT_edit_bitmask(bpy.types.Operator):
    """Modal dialog for toggling the 8 individual boolean flags of a BITMASK schema slot"""
    bl_idname = "b2u.edit_bitmask"
    bl_label = "Edit Bitmask Flags"
    bl_options = {'REGISTER', 'UNDO'}

    mat_index: bpy.props.IntProperty()
    slot_id: bpy.props.StringProperty()

    bit_0: bpy.props.BoolProperty()
    bit_1: bpy.props.BoolProperty()
    bit_2: bpy.props.BoolProperty()
    bit_3: bpy.props.BoolProperty()
    bit_4: bpy.props.BoolProperty()
    bit_5: bpy.props.BoolProperty()
    bit_6: bpy.props.BoolProperty()
    bit_7: bpy.props.BoolProperty()

    def invoke(self, context, event):
        try:
            mat = context.scene.b2u_materials[self.mat_index]
            block = mat.data_blocks.get(self.slot_id)
            val = block.i_val if block else 0
            for i in range(8):
                setattr(self, f"bit_{i}", bool(val & (1 << i)))
        except (IndexError, AttributeError):
            pass
        return context.window_manager.invoke_props_dialog(self, width=280)

    def draw(self, context):
        try:
            mat = context.scene.b2u_materials[self.mat_index]
            schema_lookup = 'OPAQUE' if mat.workflow == 'OPAQUE_DS' else mat.workflow
            s_def = next(
                (s for s in context.scene.b2u_settings.slot_defs if s.schema == schema_lookup and s.slot_id == self.slot_id),
                None,
            )
            if not s_def:
                return

            names = [n.strip() for n in s_def.bitmask_items.split(',')]
            self.layout.label(text=f"Flags for: {s_def.ui_name}", icon='PREFERENCES')

            box = self.layout.box()
            drawn = False
            for i in range(min(8, len(names))):
                if names[i]:
                    box.prop(self, f"bit_{i}", text=names[i])
                    drawn = True
            if not drawn:
                box.label(text="No flags named in Schema Configurator.", icon='INFO')
        except (IndexError, AttributeError):
            pass

    def execute(self, context):
        try:
            mat = context.scene.b2u_materials[self.mat_index]
            block = mat.data_blocks.get(self.slot_id)
            if block:
                val = 0
                for i in range(8):
                    if getattr(self, f"bit_{i}"):
                        val |= (1 << i)
                block.i_val = val
                from .data_properties import mark_dirty
                mark_dirty(self, context)
        except (IndexError, AttributeError):
            pass
        return {'FINISHED'}


class B2U_OT_add_material(bpy.types.Operator):
    """Adds a new material slot to the palette at the lowest available LUT row"""
    bl_idname = "b2u.add_mat"
    bl_label = "Add Material"
    bl_description = "Add a material to the palette. It will automatically be inserted at the lowest possible ID"

    def execute(self, context):
        from .data_properties import ALL_SLOT_KEYS, find_free_row_index, mark_dirty

        scene = context.scene
        free_row = find_free_row_index(scene, 1)
        if free_row == -1:
            self.report({'ERROR'}, "Palette LUT is full (256/256 rows allocated)!")
            return {'CANCELLED'}

        new_mat = scene.b2u_materials.add()
        new_mat.allocated_rows = 1
        new_mat.start_row_index = free_row

        for _ in range(8):
            new_mat.dynamic_colors.add()
            new_mat.variant_colors.add()

        target_schema = 'OPAQUE' if new_mat.workflow == 'OPAQUE_DS' else new_mat.workflow
        slot_lookup = {
            s.slot_id: s
            for s in scene.b2u_settings.slot_defs
            if s.schema == target_schema
        }

        for key in ALL_SLOT_KEYS:
            b = new_mat.data_blocks.add()
            b.name = key
            b.schema_type = target_schema
            s_def = slot_lookup.get(key)
            if s_def:
                if s_def.data_type == 'FLOAT':
                    b.f_val = s_def.default_val
                elif s_def.data_type in {'INT', 'BITMASK'}:
                    b.i_val = int(s_def.default_val)
                elif s_def.data_type == 'BOOL':
                    b.b_val = bool(s_def.default_b_val)
                elif s_def.data_type == 'ENUM':
                    b.e_val = str(int(s_def.default_val))

        scene.b2u_active_mat_index = len(scene.b2u_materials) - 1
        mark_dirty(self, context)
        return {'FINISHED'}


class B2U_OT_remove_material(bpy.types.Operator):
    """Removes the currently selected material slot from the palette"""
    bl_idname = "b2u.remove_mat"
    bl_label = "Remove Material"
    bl_description = "Deletes the selected material from the palette. Does not rearrange IDs"

    def execute(self, context):
        from .data_properties import mark_dirty

        scene = context.scene
        idx = scene.b2u_active_mat_index
        if len(scene.b2u_materials) > 0:
            scene.b2u_materials.remove(idx)
            scene.b2u_active_mat_index = min(max(0, idx - 1), len(scene.b2u_materials) - 1)
            mark_dirty(self, context)
        return {'FINISHED'}


class B2U_PT_palette_master(bpy.types.Panel):
    """Scene Properties panel for managing the B2U palette, schema definitions, and material slots."""
    bl_label = "B2U Palette Manager"
    bl_idname = "B2U_PT_palette_master"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "scene"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.b2u_settings
        addon_name = __package__ or "b2u_material_master"
        addon_prefs = context.preferences.addons.get(addon_name)

        if not addon_prefs:
            layout.label(text="Preferences not found. Please restart or re-enable the addon.", icon='ERROR')
            return

        prefs = addon_prefs.preferences

        # Project & Texture Configuration
        box = layout.box()
        box.label(text="Project Configuration", icon='FILE_FOLDER')
        box.prop(prefs, "unity_export_path")
        box.prop(prefs, "json_palette_dir")

        box.separator(factor=0.5)
        box.label(text="Global Detail Atlas:", icon='TEXTURE')
        box.template_ID(settings, "detail_atlas_image", open="image.open")
        #if settings.detail_atlas_image:
        #    row = box.row(align=True)
        #    row.prop(settings, "detail_atlas_width")
        #    row.prop(settings, "detail_atlas_height")

        box.separator(factor=0.5)
        box.label(text="Global Emission Pattern:", icon='TEXTURE')
        box.template_ID(settings, "emission_pattern_image", open="image.open")

        box.separator(factor=0.5)
        box.label(text="Global Iridescence LUT:", icon='TEXTURE')
        box.template_ID(settings, "iridescence_lut_image", open="image.open")

        box.separator(factor=0.5)
        box.operator("b2u.force_refresh_data", icon='FILE_REFRESH')
        box.separator(factor=0.5)

        if not prefs.json_palette_dir or not os.path.exists(bpy.path.abspath(prefs.json_palette_dir)):
            box.alert = True
            box.label(text="Invalid Palette Folder!", icon='ERROR')
            box.label(text="Set a valid path above to continue.")
            return

        inv_row = box.row(align=True)
        inv_row.label(text="Flow Channel Inversions:")
        inv_row.prop(settings, "invert_r", toggle=True)
        inv_row.prop(settings, "invert_g", toggle=True)
        box.separator(factor=0.5)

        row = box.row(align=True)
        row.active = not settings.is_dirty
        row.prop(settings, "active_palette_file", text="Active Palette")
        row.operator("b2u.save_as_json", text="", icon='ADD')
        row.operator("b2u.reload_palette", text="", icon='FILE_REFRESH')

        box.separator()
        as_row = box.row()
        as_row.prop(settings, "enable_autosave")
        if settings.enable_autosave:
            as_row.prop(settings, "autosave_delay", text="Delay (s)")

        if settings.active_palette_file == "NONE":
            box.separator()
            box.scale_y = 1.5
            box.operator("b2u.save_as_json", text="Create New Palette", icon='FILE_NEW')
            if len(scene.b2u_materials) > 0:
                box.scale_y = 1.0
                box.alert = True
                box.operator("b2u.clear_palette_data", text="Clear Orphaned .blend Data", icon='TRASH')
            return

        if settings.is_dirty:
            dirty_box = box.box()
            if settings.enable_autosave:
                status_text = (
                    f"Last Autosave: {settings.last_autosave_string}"
                    if settings.last_autosave_string != "Never"
                    else "Pending Autosave..."
                )
                dirty_box.label(text=status_text, icon='TIME')
            else:
                dirty_box.alert = True
                dirty_box.label(text="UNSAVED CHANGES DETECTED", icon='ERROR')

            action_row = dirty_box.row()
            action_row.operator("b2u.save_schema_json", text="Save Data", icon='FILE_TICK').export_luts = False
            action_row.operator("b2u.load_schema_json", text="Discard Changes", icon='TRASH')
        else:
            clean_box = box.box()
            if settings.enable_autosave and settings.last_autosave_string != "Never":
                clean_box.label(text=f"Last Autosave: {settings.last_autosave_string}", icon='TIME')
            box.operator("b2u.save_schema_json", text="Save Palette Data", icon='FILE_TICK').export_luts = False

        box.operator("b2u.export_luts", text="Export LUTs to Unity", icon='EXPORT')

        # Schema Configurator
        box = layout.box()
        box.prop(settings, "show_schema_ui", icon='MOD_DATA_TRANSFER', text="Schema Configurator")
        if settings.show_schema_ui:
            box.prop(settings, "schema_view_mode", text="Workflow Schema")
            for s_def in settings.slot_defs:
                if s_def.schema == settings.schema_view_mode:
                    if s_def.slot_id.startswith("r2_") and settings.schema_view_mode != 'LIQUID':
                        continue

                    outer_row = box.row(align=True)
                    outer_row.prop(s_def, "is_active", text="")
                    config_col = outer_row.column(align=True)
                    config_col.active = s_def.is_active

                    top_row = config_col.row(align=True)
                    top_row.prop(s_def, "ui_name", text="")
                    top_row.prop(s_def, "data_type", text="")
                    config_col.prop(s_def, "ui_description", text="")

                    if s_def.data_type == 'ENUM':
                        enum_row = config_col.row(align=True)
                        enum_row.prop(s_def, "enum_items", text="Options (Comma-Separated)")
                        enum_row.prop(s_def, "default_val", text="Default Index")
                    elif s_def.data_type == 'BITMASK':
                        bitmask_row = config_col.row(align=True)
                        bitmask_row.prop(s_def, "bitmask_items", text="Flag Names (Comma-Separated)")
                        bitmask_row.prop(s_def, "default_val", text="Default Int (0-255)")
                    elif s_def.data_type in {'FLOAT', 'INT'}:
                        bounds_row = config_col.row(align=True)
                        bounds_row.prop(s_def, "min_val", text="Min")
                        bounds_row.prop(s_def, "max_val", text="Max")
                        bounds_row.prop(s_def, "default_val", text="Default")
                    elif s_def.data_type == 'BOOL':
                        config_col.prop(s_def, "default_b_val", text="Default State")
                    box.separator(factor=0.5)

        # Biome Settings
        box = layout.box()
        box.prop(settings, "show_naming_ui", icon='PREFERENCES', text="Biome Settings")
        if settings.show_naming_ui:
            box.row().prop(settings, "num_subtypes")
            box.separator()
            box.label(text="Name Definitions:", icon='OUTLINER_DATA_FONT')
            grid_row = box.row()
            col1, col2 = grid_row.column(), grid_row.column()
            col1.label(text="Biomes")
            for i in range(min(8, len(settings.biome_names))):
                col1.prop(settings.biome_names[i], "name", text=f"{i}")
            col2.label(text="Subtypes")
            for i in range(min(settings.num_subtypes, len(settings.subtype_names))):
                col2.prop(settings.subtype_names[i], "name", text=f"{i}")

        # Material List & Active Material Editor
        layout.separator()
        used_rows = sum(m.allocated_rows for m in scene.b2u_materials)
        layout.row().label(
            text=f"LUT Memory: {used_rows} / 256 Slots Allocated",
            icon='MEMORY' if used_rows < 250 else 'ERROR',
        )

        layout.template_list("B2U_UL_material_list", "", scene, "b2u_materials", scene, "b2u_active_mat_index", rows=5)

        col = layout.column(align=True)
        add_col = col.column(align=True)
        add_col.active = (used_rows < 256)
        add_col.operator("b2u.add_mat", icon='ADD', text="")
        col.operator("b2u.remove_mat", icon='REMOVE', text="")

        if len(scene.b2u_materials) == 0:
            return
        active_idx = min(max(0, scene.b2u_active_mat_index), len(scene.b2u_materials) - 1)
        mat = scene.b2u_materials[active_idx]

        layout.separator()
        header_row = layout.row(align=True)
        header_row.prop(mat, "mat_name", text="")
        header_row.prop(mat, "is_locked", icon='LOCKED' if mat.is_locked else 'UNLOCKED', text="", toggle=True)

        if mat.is_locked:
            layout.label(text="Material is locked. Unlock to edit properties and colors.", icon='LOCKED')
        else:
            mat_body = layout.column()
            row = mat_body.row()
            row.prop(mat, "workflow")
            row.prop(mat, "biome_type")
            row.prop(mat, "start_row_index", text="Row ID")

            draw_material_schema_properties(mat_body, scene, settings, mat, active_idx)
            draw_material_color_properties(mat_body, settings, mat)


class B2U_PT_mesh_tools(bpy.types.Panel):
    """3D Viewport N-Panel for assigning materials and baking vertex/UV data onto meshes."""
    bl_label = "B2U Master"
    bl_idname = "B2U_PT_mesh_tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'B2U Master'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.b2u_settings
        obj = context.active_object
        addon_name = __package__ or "b2u_material_master"
        addon_prefs = context.preferences.addons.get(addon_name)

        if not addon_prefs:
            layout.label(text="Preferences not found. Please restart or re-enable the addon.", icon='ERROR')
            return

        prefs = addon_prefs.preferences

        def draw_header(box_layout, prop_name, title, icon_name):
            row = box_layout.row(align=True)
            is_open = getattr(settings, prop_name)
            row.prop(
                settings,
                prop_name,
                text="",
                icon='DISCLOSURE_TRI_DOWN' if is_open else 'DISCLOSURE_TRI_RIGHT',
                emboss=False,
            )
            row.label(text=title, icon=icon_name)
            return is_open

        box = layout.box()
        if draw_header(box, "ui_show_global", "Global Pipeline", 'PREFERENCES'):
            ws_text = "Create B2U Workspace" if "B2U Palette" not in bpy.data.workspaces else "Open B2U Workspace"
            box.operator("b2u.setup_workspace", text=ws_text, icon='WORKSPACE')

        if (
            not prefs.json_palette_dir
            or not os.path.exists(bpy.path.abspath(prefs.json_palette_dir))
            or settings.active_palette_file == "NONE"
        ):
            box = layout.box()
            box.alert = True
            box.label(text="No Active Palette!", icon='ERROR')
            box.label(text="Please configure your palette in")
            box.label(text="the global pipeline.")
            return

        box = layout.box()
        if draw_header(box, "ui_show_preview", "Viewport Preview", 'SHADING_RENDERED'):
            preview_text = "Disable Live Preview" if settings.enable_live_preview else "Enable Live Preview"
            box.prop(settings, "enable_live_preview", toggle=True, icon='SHADING_RENDERED', text=preview_text)
            if settings.enable_live_preview:
                v_row = box.row()
                v_row.prop(settings, "preview_biome", text="Biome")
                v_row.prop(settings, "preview_subtype", text="Subtype")
                c_row = box.row()
                c_row.prop(settings, "preview_curvature_strength", slider=True)
                c_row.prop(settings, "preview_curvature_power", slider=True)
                e_row = box.row()
                e_row.prop(settings, "preview_emission_strength", slider=True)
                e_row.prop(settings, "preview_emission_speed", slider=True)
                n_row = box.row()
                n_row.prop(settings, "preview_noise_strength", slider=True)
                n_row.prop(settings, "preview_noise_scale", slider=True)
                t_row = box.row()
                t_row.prop(settings, "preview_triplanar_blend_power", slider=True)
                t_row.prop(settings, "preview_show_paint", toggle=True)

        if not obj or obj.type not in {'MESH', 'CURVE'}:
            return

        if obj.type == 'CURVE':
            box = layout.box()
            if draw_header(box, "ui_show_flow", "Curve Flow Tools", 'CURVE_DATA'):
                box.operator("b2u.reverse_curve", icon='ARROW_LEFTRIGHT')
                box.label(text="Tip: Press Tab to select and reverse", icon='INFO')
                box.label(text="individual forks within the curve.")
            return

        box = layout.box()
        if draw_header(box, "ui_show_workflow", "Object Workflow", 'MESH_DATA'):
            box.prop(obj, "b2u_workflow", text="")

        if obj.b2u_workflow == 'NONE':
            layout.box().label(text="Select a workflow to initialize B2U tools.", icon='INFO')
            return

        # Material Assigner
        box = layout.box()
        if draw_header(box, "ui_show_assigner", "Material Assigner (Vertex A)", 'MATERIAL'):
            mat_row = box.row(align=True)
            mat_row.prop(settings, "active_assigner_mat_id", text="")
            mat_row.operator("b2u.quick_edit_material", text="", icon='PREFERENCES')

            mat_id = settings.active_assigner_mat_id
            if mat_id not in {"NONE", "-1"}:
                try:
                    mat = scene.b2u_materials[int(mat_id)]
                    row = box.row()
                    if mat.biome_type == 'INDEP_DYN':
                        row.prop(settings, "assigner_biome_response", text="Response")
                        if settings.assigner_biome_response == 'INDEPENDENT' and mat.workflow not in {'CRYSTAL', 'LIQUID'}:
                            row.prop(settings, "assigner_variant", text="Color Variant")
                    else:
                        row.active = False
                        resp_label = "Independent (Uniform)" if mat.biome_type == 'INDEPENDENT' else "Dynamic (Biome)"
                        row.label(text=f"Response: {resp_label}", icon='INFO')
                        if mat.biome_type == 'INDEPENDENT' and mat.workflow not in {'CRYSTAL', 'LIQUID'}:
                            box.row(align=True).prop(settings, "assigner_variant", text="Color Variant")
                except (ValueError, IndexError):
                    pass

            is_liquid = (obj.b2u_workflow == 'LIQUID')
            if is_liquid:
                box.separator()
                box.label(text="Liquid Mesh Properties:", icon='MOD_FLUIDSIM')

                row_type = box.row(align=True)
                row_type.prop(obj, "b2u_liq_tessellated", toggle=True, icon='MOD_SUBSURF')
                row_type.prop(obj, "b2u_liq_volumetric", toggle=True, icon='LIGHTPROBE_VOLUME')

                col = box.column(align=True)
                col.prop(obj, "b2u_liq_displacement")
                col.prop(obj, "b2u_liq_transmissive")
                col.prop(obj, "b2u_liq_ignore_gravity")
                col.prop(obj, "b2u_liq_caustics")
                col.prop(obj, "b2u_liq_invert_height")
                col.prop(obj, "b2u_liq_height_transmission")
                col.prop(obj, "b2u_liq_height_emission")

                box.separator()
                box.operator("b2u.fill_material_id", text="Fill Entire Object", icon='BRUSH_DATA')
            else:
                is_edit = (context.mode == 'EDIT_MESH')
                box.operator(
                    "b2u.assign_material_faces" if is_edit else "b2u.fill_material_id",
                    text="Assign to Faces (CTRL+A)" if is_edit else "Fill Entire Object",
                    icon='RESTRICT_SELECT_OFF' if is_edit else 'BRUSH_DATA',
                )

        # Solid / Crystal / Transparent Mesh Tools
        if obj.b2u_workflow in {'OPAQUE', 'OPAQUE_DS', 'TRANSPARENT', 'CRYSTAL'}:
            box = layout.box()
            if draw_header(box, "ui_show_uv_tools", "Texture Scale & Rotation (UV Data)", 'UV'):
                if obj.b2u_workflow == 'CRYSTAL':
                    box.label(text="For crystal workflows, the UV projection only affects the emission pattern.")
                uv_row = box.row(align=True)
                uv_row.operator("b2u.set_grain_vector", text="Set Custom Vector", icon='VERTEXSEL')
                uv_row.prop(settings, "grad_show_custom", toggle=True, icon='HIDE_OFF', text="")
                box.separator()
                box.prop(settings, "grain_axis", text="Direction")
                box.prop(settings, "grain_reverse")
                box.row(align=True).prop(settings, "grain_scale", text="Per-Face Scale")
                box.separator()

                if settings.grain_axis == 'AUTO':
                    op_grain = box.operator("b2u.bake_automatic_grain", text="Auto-Bake Volume Grain", icon='OUTLINER_OB_VOLUME')
                else:
                    op_grain = box.operator("b2u.bake_grain_uvs", text="Bake Grain & Scale", icon='FILE_REFRESH')
                    op_grain.axis = settings.grain_axis
                op_grain.reverse = settings.grain_reverse
                op_grain.scale_x = settings.grain_scale[0]
                op_grain.scale_y = settings.grain_scale[1]

            if obj.b2u_workflow != 'CRYSTAL':
                box = layout.box()
                if draw_header(box, "ui_show_edgewear", "Edge Wear Thickness (UV Data)", 'MODIFIER'):
                    box.prop(settings, "auto_bake_edgewear", toggle=True, icon='AUTO')
                    if settings.auto_bake_edgewear:
                        box.label(text="Distance Field auto-bakes on leaving Edit Mode.", icon='INFO')
                    else:
                        box.operator("b2u.bake_physical_distance", text="Force Bake Distance Field", icon='FILE_REFRESH')
                    
                    box.operator("b2u.clear_physical_distance", text="Clear Distance Field", icon='X')
                    
                    box.separator()
                    box.label(text="Live Parameters:", icon='PROPERTIES')
                    box.prop(settings, "edge_wear_angle", text="Sharpness Threshold")
                    box.prop(settings, "edge_wear_thickness", text="Global Thickness", slider=True)
                    box.separator()

                    op_ang = box.operator("b2u.apply_edge_thickness", text="Bake Angle-Based Thickness", icon='MOD_BEVEL')
                    op_ang.apply_mode = 'ANGLE'
                    op_ang.thickness_value = settings.edge_wear_thickness

                    op_uni = box.operator("b2u.apply_edge_thickness", text="Apply Uniform Thickness", icon='BRUSH_DATA')
                    op_uni.apply_mode = 'UNIFORM'
                    op_uni.thickness_value = settings.edge_wear_thickness

                    if context.mode == 'EDIT_MESH':
                        box.separator()
                        box.label(text="Manual Edit Mode Tools:")
                        op_man = box.operator("b2u.apply_edge_thickness", text="Apply Thickness to Selected", icon='BRUSH_DATA')
                        op_man.apply_mode = 'MANUAL'
                        op_man.thickness_value = settings.edge_wear_thickness

                p_box = layout.box()
                if draw_header(p_box, "ui_show_paint", "Paint Masking (Vertex B)", 'BRUSH_DATA'):
                    p_row = p_box.row(align=True)
                    p_row.prop(settings, "assigner_paint_val", text="Strength")
                    is_edit = (context.mode == 'EDIT_MESH')
                    p_row.operator(
                        "b2u.assign_paint_mask" if is_edit else "b2u.fill_paint_mask",
                        text="Apply to Selection" if is_edit else "Fill Entire Object",
                    )

            e_box = layout.box()
            if draw_header(e_box, "ui_show_emission", "Emission Painting (Vertex G)", 'LIGHT'):
                e_row = e_box.row(align=True)
                e_row.prop(settings, "assigner_emission_val", text="Strength")
                is_edit = (context.mode == 'EDIT_MESH')
                e_row.operator(
                    "b2u.assign_emission" if is_edit else "b2u.fill_emission",
                    text="Apply to Selection" if is_edit else "Fill Entire Object",
                )

            if obj.b2u_workflow in {'OPAQUE', 'OPAQUE_DS', 'CRYSTAL'}:
                g_box = layout.box()
                if draw_header(g_box, "ui_show_gradient", "Gradient Baking (Vertex R)", 'COLOR'):
                    g_row = g_box.row(align=True)
                    g_row.operator("b2u.clear_gradient", text="Clear Gradient", icon='X')
                    g_box.prop(settings, "grad_axis", text="Direction")

                    if settings.grad_axis == 'CUSTOM AXIS':
                        c_row = g_box.row(align=True)
                        c_row.operator("b2u.set_gradient_vector", text="Set from 2 Verts", icon='VERTEXSEL')
                        c_row.prop(settings, "grad_show_custom", toggle=True, icon='HIDE_OFF')
                        if context.mode != 'EDIT_MESH':
                            g_box.label(text="Requires Edit Mode to set vector", icon='INFO')
                    elif settings.grad_axis == 'CURVE':
                        g_box.prop(settings, "grad_curve", text="Curve Object")
                    elif settings.grad_axis == 'DEPTH':
                        g_box.label(text="Volumetric Depth Projection:", icon='EMPTY_SINGLE_ARROW')
                        g_box.prop(settings, "alpha_bake_axis", text="Ray Direction")
                        g_box.prop(settings, "alpha_bake_max_depth", text="Max Depth")

                    if settings.grad_axis != 'DEPTH':
                        bounds_row = g_box.row(align=True)
                        if settings.grad_axis == 'CURVE':
                            bounds_row.prop(settings, "grad_curve_min", text="Start Eval")
                            bounds_row.prop(settings, "grad_curve_max", text="End Eval")
                        else:
                            bounds_row.prop(settings, "grad_bounds_min", text="Start")
                            bounds_row.prop(settings, "grad_bounds_max", text="End")

                    g_box.prop(settings, "grad_reverse", text="Reverse 0-1 Direction")

                    if settings.grad_axis == 'DEPTH':
                        op_depth = g_box.operator("b2u.bake_thickness_alpha", text="Bake Depth", icon='MOD_THICKNESS')
                        op_depth.axis_mode = settings.alpha_bake_axis
                        op_depth.max_dist = settings.alpha_bake_max_depth
                        op_depth.reverse = settings.grad_reverse
                    else:
                        op_grad = g_box.operator("b2u.bake_gradient", text="Bake Gradient", icon='MOD_HUE_SATURATION')
                        op_grad.axis = settings.grad_axis
                        op_grad.reverse = settings.grad_reverse
                        if settings.grad_axis == 'CURVE':
                            op_grad.bounds_min = settings.grad_curve_min
                            op_grad.bounds_max = settings.grad_curve_max
                            if settings.grad_curve:
                                op_grad.curve_name = settings.grad_curve.name
                        else:
                            op_grad.bounds_min = settings.grad_bounds_min
                            op_grad.bounds_max = settings.grad_bounds_max
            else:
                a_box = layout.box()
                if draw_header(a_box, "ui_show_alpha", "Transparency (R Channel)", 'BRUSH_DATA'):
                    a_row = a_box.row(align=True)
                    a_row.prop(settings, "assigner_alpha_val", text="Strength")
                    is_edit = (context.mode == 'EDIT_MESH')
                    a_row.operator(
                        "b2u.assign_alpha" if is_edit else "b2u.fill_alpha",
                        text="Apply to Selection" if is_edit else "Fill Entire Object",
                    )
                    a_box.separator()
                    a_box.label(text="Volumetric Depth Projection:", icon='EMPTY_SINGLE_ARROW')
                    a_box.prop(settings, "alpha_bake_axis", text="Direction")
                    a_box.prop(settings, "alpha_bake_max_depth")
                    a_box.prop(settings, "alpha_bake_reverse")
                    op_alpha = a_box.operator("b2u.bake_thickness_alpha", text="Bake Mesh Thickness", icon='MOD_THICKNESS')
                    op_alpha.axis_mode = settings.alpha_bake_axis
                    op_alpha.max_dist = settings.alpha_bake_max_depth
                    op_alpha.reverse = settings.alpha_bake_reverse

        # Liquid Flow Pipeline
        elif obj.b2u_workflow == 'LIQUID':
            box = layout.box()
            mat_valid = False
            mat_id_str = settings.active_assigner_mat_id
            if mat_id_str not in {"NONE", "-1"}:
                try:
                    mat = scene.b2u_materials[int(mat_id_str)]
                    if mat.workflow == 'LIQUID':
                        mat_valid = True
                except (ValueError, IndexError):
                    pass

            if not mat_valid:
                box.label(text="Assign a Liquid Material first!", icon='ERROR')
                box.label(text="Configure your materials in the Master Panel.")
            elif draw_header(box, "ui_show_flow", "Fluid Flow Pipeline", 'BRUSH_DATA'):
                box.label(text="Live Flow Settings:", icon='OUTLINER_OB_CURVE')
                mod = obj.modifiers.get("B2U_LiveFlow_Engine")

                if mod:
                    box.separator()
                    sock_map = get_gn_socket_map(mod.node_group)

                    curve_id = sock_map.get("Flow Curve")
                    str_id = sock_map.get("Flow Strength")
                    falloff_toggle_id = sock_map.get("Use Curve Falloff")
                    falloff_dist_id = sock_map.get("Curve Falloff Dist")
                    col_id = sock_map.get("Collisions")
                    col_inf_id = sock_map.get("Collision Influence")
                    pres_id = sock_map.get("Pressure Factor")
                    z_thresh_id = sock_map.get("Surface Hover Threshold")
                    flow_smooth_id = sock_map.get("Flow Smoothing")

                    if curve_id:
                        box.prop(mod, f'["{curve_id}"]', text="Target Curve")
                    if str_id:
                        box.prop(mod, f'["{str_id}"]', text="Flow Strength")

                    box.separator()
                    if falloff_toggle_id:
                        box.prop(mod, f'["{falloff_toggle_id}"]', text="Curve Distance Falloff")
                    if falloff_dist_id:
                        f_row = box.row()
                        f_row.active = bool(mod.get(falloff_toggle_id, False))
                        f_row.prop(mod, f'["{falloff_dist_id}"]', text="Falloff Distance")

                    box.separator()
                    if col_id:
                        box.prop(mod, f'["{col_id}"]', text="Collisions")
                    if col_inf_id:
                        box.prop(mod, f'["{col_inf_id}"]', text="Collision Influence")
                    if pres_id:
                        box.prop(mod, f'["{pres_id}"]', text="Pressure Factor")
                    if z_thresh_id:
                        box.prop(mod, f'["{z_thresh_id}"]', text="Surface Hover Threshold")
                    if flow_smooth_id:
                        box.prop(mod, f'["{flow_smooth_id}"]', text="Flow Smoothing")

                    box.separator()
                    row = box.row(align=True)
                    row.alert = True
                    row.operator("b2u.add_live_flow_modifier", text="Force Refresh Tangents", icon='FILE_REFRESH')
                    row.alert = False
                    row.operator("b2u.flow_reset", text="Remove Engine", icon='TRASH')
                else:
                    box.separator()
                    box.label(text="Initial Flow Settings:", icon='OUTLINER_OB_CURVE')
                    box.prop(settings, "flow_target_curve", text="Target Curve")
                    box.prop(settings, "flow_collision_collection", text="Collisions")
                    box.separator()
                    if settings.flow_target_curve:
                        box.operator("b2u.add_live_flow_modifier", text="Attach Live Flow Engine", icon='MOD_FLUIDSIM')
                    else:
                        box.label(text="Select a Curve to attach the engine!", icon='INFO')

                layout.separator()
                vis = box.box()
                vis.prop(settings, "flow_show_vectors", toggle=True, icon='VIEW3D')
                if settings.flow_show_vectors:
                    col = vis.column(align=True)
                    col.prop(settings, "flow_vector_length", slider=True)
                    col.prop(settings, "flow_vector_color", text="")


class B2U_OT_quick_edit_material(bpy.types.Operator):
    """Pop-up dialog in the 3D Viewport for editing the active assigner material's properties and colors"""
    bl_idname = "b2u.quick_edit_material"
    bl_label = "Quick Edit Material"
    bl_description = "Quickly change color and property settings for the selected material"

    def invoke(self, context, event):
        if context.scene.b2u_settings.active_assigner_mat_id in {"NONE", "-1"}:
            self.report({'WARNING'}, "No active material to edit!")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.b2u_settings
        try:
            mat_idx = int(settings.active_assigner_mat_id)
            mat = context.scene.b2u_materials[mat_idx]
        except (ValueError, IndexError):
            layout.label(text="Material Data Missing", icon='ERROR')
            return

        layout.label(text=f"Editing: {mat.mat_name}", icon='MATERIAL')
        layout.separator()

        if mat.is_locked:
            layout.label(text="Material is locked in the Palette Manager.", icon='LOCKED')
            return

        draw_material_schema_properties(layout, context.scene, settings, mat, mat_idx)
        draw_material_color_properties(layout, settings, mat)

    def execute(self, context):
        return {'FINISHED'}

# endregion


# region Registration

classes = (
    B2U_OT_force_refresh_data,
    B2U_OT_clear_palette_data,
    B2U_Preferences,
    B2U_UL_material_list,
    B2U_OT_edit_bitmask,
    B2U_OT_add_material,
    B2U_OT_remove_material,
    B2U_PT_palette_master,
    B2U_PT_mesh_tools,
    B2U_OT_setup_workspace,
    B2U_OT_quick_edit_material,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

# endregion