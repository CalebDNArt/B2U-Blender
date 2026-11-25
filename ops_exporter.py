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

import bpy
import os

WORKFLOW_IDS = {
    'OPAQUE': 0.0,
    'OPAQUE_DS': 0.1,
    'TRANSPARENT': 0.2,
    'CRYSTAL': 0.3,
    'LIQUID': 0.4,
}


class B2U_OT_export_luts(bpy.types.Operator):
    """Bakes the current material palette and schema into 8-bit PNG LUTs for Unity"""
    bl_idname = "b2u.export_luts"
    bl_label = "Generate & Export LUTs to Unity"
    bl_description = "Bakes the current schema into 8-bit PNGs and saves them to the global Unity path"

    def execute(self, context):
        scene = context.scene
        materials = scene.b2u_materials
        settings = scene.b2u_settings

        addon_name = __package__ or "b2u_material_master"
        prefs = context.preferences.addons[addon_name].preferences
        export_dir = bpy.path.abspath(prefs.unity_export_path)

        if not export_dir or not os.path.isdir(export_dir):
            self.report({'ERROR'}, "Invalid Unity Export Path! Set it in the Addon Preferences or B2U UI.")
            return {'CANCELLED'}

        raw_name = settings.active_palette_file
        pal_name = raw_name.replace(".json", "") if raw_name != "NONE" else "Default"

        num_subtypes = settings.num_subtypes
        base_color_cols = 8 * num_subtypes
        lut_width = base_color_cols + 8
        prop_width = 4

        color_name = f"B2U_{pal_name}_ColorEmissionLUT_{lut_width}x256"
        prop_name = f"B2U_{pal_name}_PropertyLUT_{prop_width}x256"

        # Retrieve or create internal Blender images for baking
        img_color = bpy.data.images.get(color_name) or bpy.data.images.new(name=color_name, width=lut_width, height=256, alpha=True)
        img_prop = bpy.data.images.get(prop_name) or bpy.data.images.new(name=prop_name, width=prop_width, height=256, alpha=True)

        if img_color.size[0] != lut_width or img_color.size[1] != 256:
            img_color.scale(lut_width, 256)
        if img_prop.size[0] != prop_width or img_prop.size[1] != 256:
            img_prop.scale(prop_width, 256)

        img_color.colorspace_settings.name = "sRGB"
        img_prop.colorspace_settings.name = "Non-Color"

        # Flat RGBA float buffers (256 rows)
        pixels_color = [0.0] * (lut_width * 256 * 4)
        pixels_prop = [0.0] * (prop_width * 256 * 4)

        # Pre-cache active schema definitions and enum option counts for O(1) lookup
        active_slots = {}
        enum_max_indices = {}
        for s in settings.slot_defs:
            if s.is_active:
                key = (s.schema, s.slot_id)
                active_slots[key] = s
                if s.data_type == 'ENUM':
                    options = [n for n in s.enum_items.split(',') if n.strip()]
                    enum_max_indices[key] = max(1.0, float(len(options) - 1))

        def get_norm(mat, schema, slot):
            """Returns the normalized [0.0, 1.0] float value for a material's schema slot."""
            key = (schema, slot)
            s_def = active_slots.get(key)
            if not s_def:
                return 0.0

            block = mat.data_blocks.get(slot)
            if not block:
                return 0.0

            dtype = s_def.data_type
            if dtype == 'FLOAT':
                val = block.f_val
            elif dtype == 'INT':
                val = float(block.i_val)
            elif dtype == 'BOOL':
                return 1.0 if block.b_val else 0.0
            elif dtype == 'BITMASK':
                return min(max(float(block.i_val) / 255.0, 0.0), 1.0)
            elif dtype == 'ENUM':
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
            """Writes 4 RGBA pixels of normalized material properties into the Property LUT buffer."""
            prefix = "r2_" if is_row_2 else "r1_"
            row_offset = y_pos * prop_width * 4

            # Pixels 1 to 3: Standard schema properties
            for p_idx in range(3):
                p_num = p_idx + 1
                idx = row_offset + (p_idx * 4)
                pixels_prop[idx:idx + 4] = [
                    get_norm(mat, schema, f"{prefix}p{p_num}_r"),
                    get_norm(mat, schema, f"{prefix}p{p_num}_g"),
                    get_norm(mat, schema, f"{prefix}p{p_num}_b"),
                    get_norm(mat, schema, f"{prefix}p{p_num}_a"),
                ]

            # Pixel 4: R channel stores workflow/biome ID on Row 1, or schema property on Row 2 (Liquids)
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
            """Writes base/depth colors across 8*N subtype columns and emission colors across the final 8 columns."""
            row_offset = y_pos * lut_width * 4
            lock_to_first_variant = (not is_dynamic) and (mat.workflow in {'LIQUID', 'CRYSTAL'})
            color_collection = mat.dynamic_colors if is_dynamic else mat.variant_colors
            col_len = len(color_collection)

            # 1. Base / Depth Color Columns (8 Biomes/Variants * num_subtypes)
            for b_idx in range(8):
                lookup_idx = 0 if lock_to_first_variant else b_idx
                has_data = lookup_idx < col_len
                c_block = color_collection[lookup_idx] if has_data else None

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

            # 2. Emission Columns (Final 8 pixels in the row)
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

        # Populate LUT buffers for each material in the palette
        for mat in materials:
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

                # Dual-mode materials write their dynamic counterpart on the subsequent row
                if mat.biome_type == 'INDEP_DYN':
                    write_prop_row(mat, schema, current_y + 1, is_row_2=False, is_dynamic_row=True)
                    write_color_row(mat, current_y + 1, True, a1_norm, a2_norm)

            elif mat.workflow == 'LIQUID':
                is_dyn_base = (mat.biome_type == 'DYNAMIC')
                r1_ca = get_norm(mat, schema, "r1_ca")
                r1_ea = get_norm(mat, schema, "r1_ea")
                r2_ca = get_norm(mat, schema, "r2_ca")
                r2_ea = get_norm(mat, schema, "r2_ea")

                # Surface Row (Row 1) and Depth Row (Row 2)
                write_prop_row(mat, schema, current_y, is_row_2=False, is_dynamic_row=is_dyn_base)
                write_prop_row(mat, schema, current_y + 1, is_row_2=True, is_dynamic_row=is_dyn_base)
                write_color_row(mat, current_y, is_dyn_base, r1_ca, r1_ea, is_liquid_depth=False)
                write_color_row(mat, current_y + 1, is_dyn_base, r2_ca, r2_ea, is_liquid_depth=True)

                # 4-row Dual-Mode Liquid (Rows 1-2 Independent, Rows 3-4 Dynamic)
                if mat.biome_type == 'INDEP_DYN' and (current_y + 3) < 256:
                    write_prop_row(mat, schema, current_y + 2, is_row_2=False, is_dynamic_row=True)
                    write_prop_row(mat, schema, current_y + 3, is_row_2=True, is_dynamic_row=True)
                    write_color_row(mat, current_y + 2, True, r1_ca, r1_ea, is_liquid_depth=False)
                    write_color_row(mat, current_y + 3, True, r2_ca, r2_ea, is_liquid_depth=True)

        # Push buffers to Blender images and write to disk
        img_color.pixels.foreach_set(pixels_color)
        img_prop.pixels.foreach_set(pixels_prop)

        img_color.filepath_raw = os.path.join(export_dir, f"{color_name}.png")
        img_color.file_format = 'PNG'
        img_color.save()

        img_prop.filepath_raw = os.path.join(export_dir, f"{prop_name}.png")
        img_prop.file_format = 'PNG'
        img_prop.save()

        self.report({'INFO'}, f"Exported LUTs to {export_dir}")
        return {'FINISHED'}


classes = (B2U_OT_export_luts,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)