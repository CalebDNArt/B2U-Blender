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
import json
import os


def get_json_dir(context):
    """Returns the absolute directory path for JSON palettes configured in Addon Preferences."""
    addon_name = __package__ or "b2u_material_master"
    if addon_name in context.preferences.addons:
        prefs = context.preferences.addons[addon_name].preferences
        dir_path = prefs.json_palette_dir
        if dir_path:
            return bpy.path.abspath(dir_path)
    return None


def get_clean_image_path(img):
    """Safely extracts and normalizes the absolute file path from a Blender Image datablock."""
    if img and getattr(img, "filepath", ""):
        abs_path = bpy.path.abspath(img.filepath)
        return os.path.normpath(abs_path)
    return ""


def resolve_palette_image(img_name, img_path, fallback_getter, colorspace=None, alpha_mode=None):
    """Finds an image in bpy.data, loads it from disk if missing, or falls back to the default asset."""
    img = None
    if img_name and img_name in bpy.data.images:
        img = bpy.data.images[img_name]
    elif img_path:
        abs_path = bpy.path.abspath(img_path)
        if os.path.exists(abs_path):
            try:
                img = bpy.data.images.load(abs_path, check_existing=True)
            except RuntimeError:
                img = None

    if img:
        if colorspace:
            img.colorspace_settings.name = colorspace
        if alpha_mode:
            img.alpha_mode = alpha_mode
    else:
        img = fallback_getter()

    return img


class B2U_OT_reload_palette(bpy.types.Operator):
    """Reloads the active palette from disk, or Shift+Click to resync schema definitions to defaults"""
    bl_idname = "b2u.reload_palette"
    bl_label = "Reload / Resync Palette"
    bl_description = "Click: Reload palette from disk. Shift+Click: Resync schema to default"
    bl_options = {'REGISTER', 'UNDO'}

    is_resync: bpy.props.BoolProperty(default=False)

    def invoke(self, context, event):
        if event.shift:
            self.is_resync = True
            return context.window_manager.invoke_confirm(self, event)
        self.is_resync = False
        return self.execute(context)

    def execute(self, context):
        if self.is_resync:
            from .data_properties import (
                initialize_b2u_settings,
                mark_dirty,
                push_schema_bounds_to_shaders,
                update_shader_preview_nodes,
            )
            from .ops_meshtools import build_blender_preview_lut

            settings = context.scene.b2u_settings
            settings.is_loading = True
            try:
                settings.slot_defs.clear()
                initialize_b2u_settings(context.scene)
                settings.palette_uses_default_schema = True

                slot_lookup = {(s.schema, s.slot_id): s for s in settings.slot_defs}
                for mat in context.scene.b2u_materials:
                    schema = 'OPAQUE' if mat.workflow == 'OPAQUE_DS' else mat.workflow
                    for block in mat.data_blocks:
                        block.schema_type = schema
                        block.f_val = block.f_val
                        block.i_val = block.i_val
                        s_def = slot_lookup.get((schema, block.name))
                        if s_def and s_def.data_type == 'ENUM':
                            try:
                                cur_idx = int(block.e_val)
                            except (ValueError, TypeError):
                                cur_idx = int(s_def.default_val)
                            clamped = min(max(cur_idx, 0), int(s_def.max_val))
                            block.e_val = str(clamped)
            finally:
                settings.is_loading = False

            build_blender_preview_lut()
            push_schema_bounds_to_shaders(context)
            update_shader_preview_nodes(None, context)
            mark_dirty(self, context)

            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()

            self.report({'INFO'}, "Schema Resynced and Values Clamped!")
        else:
            bpy.ops.b2u.load_schema_json()
            self.report({'INFO'}, "Palette Reloaded from Disk!")

        return {'FINISHED'}


class B2U_OT_save_schema_json(bpy.types.Operator):
    """Serializes the current palette settings, schema definitions, and materials to a JSON file"""
    bl_idname = "b2u.save_schema_json"
    bl_label = "Save Data"
    bl_description = "Save current palette and optionally export the LUT textures to Unity"

    export_luts: bpy.props.BoolProperty(name="Export LUTs to Unity", default=True)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Palette Data will be saved to disk.", icon='INFO')
        layout.prop(self, "export_luts")

    def execute(self, context):
        scene = context.scene
        settings = scene.b2u_settings
        dir_path = get_json_dir(context)
        file_name = settings.active_palette_file

        if not dir_path or not os.path.isdir(dir_path):
            self.report({'ERROR'}, "Invalid JSON Palette Directory!")
            return {'CANCELLED'}

        if not file_name or file_name == "NONE":
            self.report({'ERROR'}, "No active palette file selected to save!")
            return {'CANCELLED'}

        path = os.path.join(dir_path, file_name)

        det_img = settings.detail_atlas_image
        emis_img = settings.emission_pattern_image
        irid_img = settings.iridescence_lut_image

        data = {
            "settings": {
                "palette_uses_default_schema": settings.palette_uses_default_schema,
                "num_subtypes": settings.num_subtypes,
                "biome_names": [b.name for b in settings.biome_names],
                "subtype_names": [s.name for s in settings.subtype_names],
                "invert_r": settings.invert_r,
                "invert_g": settings.invert_g,
                "invert_b": settings.invert_b,
                "detail_atlas_width": settings.detail_atlas_width,
                "detail_atlas_height": settings.detail_atlas_height,
                "noise_atlas_dimensions": getattr(settings, "noise_atlas_dimensions", 64),
                "detail_atlas_image_name": det_img.name if det_img else "",
                "detail_atlas_image_path": get_clean_image_path(det_img),
                "emission_pattern_image_name": emis_img.name if emis_img else "",
                "emission_pattern_image_path": get_clean_image_path(emis_img),
                "iridescence_lut_image_name": irid_img.name if irid_img else "",
                "iridescence_lut_image_path": get_clean_image_path(irid_img),
            },
            "schema_definitions": [],
            "materials": [],
        }

        was_loading = settings.is_loading
        settings.is_loading = True
        try:
            for s in settings.slot_defs:
                if s.data_type == 'ENUM':
                    options = [n.strip() for n in s.enum_items.split(',') if n.strip()]
                    min_val = 0.0
                    max_val = float(max(0, len(options) - 1))
                    default_val = float(min(max(int(round(s.default_val)), 0), int(max_val)))

                    # Keep the Blender scene slot definition synchronized with the serialized bounds
                    if s.min_val != min_val:
                        s.min_val = min_val
                    if s.max_val != max_val:
                        s.max_val = max_val
                    if s.default_val != default_val:
                        s.default_val = default_val
                else:
                    min_val = s.min_val
                    max_val = s.max_val
                    default_val = s.default_val

                data["schema_definitions"].append({
                    "schema": s.schema,
                    "slot_id": s.slot_id,
                    "ui_name": s.ui_name,
                    "ui_description": s.ui_description,
                    "data_type": s.data_type,
                    "enum_items": s.enum_items,
                    "bitmask_items": s.bitmask_items,
                    "min_val": min_val,
                    "max_val": max_val,
                    "default_val": default_val,
                    "default_b_val": s.default_b_val,
                    "is_active": s.is_active,
                })
        finally:
            settings.is_loading = was_loading

        for m in scene.b2u_materials:
            mat_data = {
                "mat_name": m.mat_name,
                "workflow": m.workflow,
                "biome_type": m.biome_type,
                "start_row_index": m.start_row_index,
                "is_locked": m.is_locked,
                "allocated_rows": m.allocated_rows,
                "dynamic_colors": [
                    {
                        "emission": list(dc.emission),
                        "sec_emission": list(dc.sec_emission),
                        "bases": [list(getattr(dc, f"base_{i}")) for i in range(7)],
                        "depths": [list(getattr(dc, f"depth_{i}")) for i in range(7)],
                    }
                    for dc in m.dynamic_colors
                ],
                "variant_colors": [
                    {
                        "emission": list(vc.emission),
                        "sec_emission": list(vc.sec_emission),
                        "bases": [list(getattr(vc, f"base_{i}")) for i in range(7)],
                        "depths": [list(getattr(vc, f"depth_{i}")) for i in range(7)],
                    }
                    for vc in m.variant_colors
                ],
                "data_blocks": [],
            }

            for db in m.data_blocks:
                raw_e = db.e_val if db.e_val != "" else str(db.get("e_val", 0))
                try:
                    clean_e_val = str(max(0, int(raw_e)))
                except (ValueError, TypeError):
                    clean_e_val = "0"

                mat_data["data_blocks"].append({
                    "name": db.name,
                    "schema_type": db.schema_type,
                    "f_val": db.f_val,
                    "i_val": db.i_val,
                    "b_val": db.b_val,
                    "e_val": clean_e_val,
                })

            data["materials"].append(mat_data)

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
        except OSError as e:
            self.report({'ERROR'}, f"Failed to write JSON file: {e}")
            return {'CANCELLED'}

        settings.is_dirty = False
        self.report({'INFO'}, f"Saved B2U data to {file_name}")

        from .data_properties import update_json_mtime, set_blender_save_time
        set_blender_save_time()
        update_json_mtime(path)

        if self.export_luts:
            bpy.ops.b2u.export_luts()

        return {'FINISHED'}


class B2U_OT_save_as_json(bpy.types.Operator):
    """Creates a new JSON palette file from a selected template"""
    bl_idname = "b2u.save_as_json"
    bl_label = "Create Palette"
    bl_description = "Create a new palette and save data to it"

    new_name: bpy.props.StringProperty(name="File Name", default="New_Palette")

    source_type: bpy.props.EnumProperty(
        name="Template",
        items=[
            ('CURRENT', "Copy Current Palette", "Duplicates the active palette and its materials"),
            ('DEFAULT', "B2U Default Setup", "Creates a fresh palette with the standard B2U schema"),
            ('EMPTY', "True Empty", "Creates a completely blank palette with no predefined data"),
        ],
        default='CURRENT',
    )

    def invoke(self, context, event):
        self.new_name = "New_Palette"
        self.source_type = 'CURRENT'
        return context.window_manager.invoke_props_dialog(self, width=350)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Create a new palette file:", icon='FILE_NEW')
        layout.prop(self, "new_name")

        layout.separator()
        layout.label(text="Starting Template:")

        col = layout.column()
        col.prop(self, "source_type", expand=True)

    def execute(self, context):
        dir_path = get_json_dir(context)
        if not dir_path or not os.path.isdir(dir_path):
            self.report({'ERROR'}, "Invalid JSON Palette Directory!")
            return {'CANCELLED'}

        safe_name = self.new_name.strip().replace(" ", "_")
        if not safe_name:
            safe_name = "New_Palette"
        if not safe_name.endswith(".json"):
            safe_name += ".json"

        path = os.path.join(dir_path, safe_name)
        if os.path.exists(path):
            self.report({'ERROR'}, f"Palette '{safe_name}' already exists! Choose a different name.")
            return {'CANCELLED'}

        scene = context.scene
        settings = scene.b2u_settings

        if self.source_type in {'DEFAULT', 'EMPTY'}:
            from .data_properties import (
                ALL_SLOT_KEYS,
                initialize_b2u_settings,
                push_schema_bounds_to_shaders,
                update_shader_preview_nodes,
            )
            from .ops_meshtools import build_blender_preview_lut

            settings.is_loading = True
            try:
                scene.b2u_materials.clear()
                scene.b2u_active_mat_index = 0
                settings.slot_defs.clear()
                settings.biome_names.clear()
                settings.subtype_names.clear()
                settings.num_subtypes = 3
                settings.invert_r = False
                settings.invert_g = False
                settings.invert_b = False

                if self.source_type == 'DEFAULT':
                    initialize_b2u_settings(scene)
                    settings.palette_uses_default_schema = True
                elif self.source_type == 'EMPTY':
                    settings.palette_uses_default_schema = False
                    while len(settings.biome_names) < 8:
                        settings.biome_names.add().name = "Unnamed Biome"
                    while len(settings.subtype_names) < 7:
                        settings.subtype_names.add().name = "Unnamed Subtype"

                    for schema in ("OPAQUE", "TRANSPARENT", "CRYSTAL", "LIQUID"):
                        for key in ALL_SLOT_KEYS:
                            new_def = settings.slot_defs.add()
                            new_def.schema = schema
                            new_def.slot_id = key
                            new_def.ui_name = "Unnamed Slot"
                            new_def.ui_description = ""
                            new_def.data_type = 'FLOAT'
                            new_def.min_val = 0.0
                            new_def.max_val = 1.0
                            new_def.default_val = 0.0
                            new_def.is_active = False
            finally:
                settings.is_loading = False

            build_blender_preview_lut()
            push_schema_bounds_to_shaders(context)
            update_shader_preview_nodes(None, context)

        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({}, f)
        except OSError as e:
            self.report({'ERROR'}, f"Could not create palette file: {e}")
            return {'CANCELLED'}

        settings.is_loading = True
        try:
            settings.active_palette_file = safe_name
        finally:
            settings.is_loading = False

        bpy.ops.b2u.save_schema_json('EXEC_DEFAULT', export_luts=False)
        return {'FINISHED'}


class B2U_OT_load_schema_json(bpy.types.Operator):
    """Discards unsaved changes and reloads the active JSON palette from disk"""
    bl_idname = "b2u.load_schema_json"
    bl_label = "Revert to Saved"
    bl_description = "Discard unsaved changes and reload the active JSON from disk"

    def execute(self, context):
        scene = context.scene
        settings = scene.b2u_settings
        dir_path = get_json_dir(context)
        file_name = settings.active_palette_file

        if not dir_path or not file_name or file_name == "NONE":
            return {'CANCELLED'}

        path = os.path.join(dir_path, file_name)
        if not os.path.exists(path):
            return {'CANCELLED'}

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self.report({'ERROR'}, f"Failed to read or parse JSON file: {file_name}")
            return {'CANCELLED'}

        from .data_properties import (
            ALL_SLOT_KEYS,
            get_default_detail_atlas,
            get_default_emission_pattern,
            get_default_iridescence_lut,
            push_schema_bounds_to_shaders,
            update_shader_preview_nodes,
            update_json_mtime,
            push_shader_uniforms
        )
        from .ops_meshtools import build_blender_preview_lut

        settings.is_loading = True

        try:
            s_data = data.get("settings", {})
            settings.palette_uses_default_schema = s_data.get("palette_uses_default_schema", True)
            settings.num_subtypes = s_data.get("num_subtypes", 3)

            # Ensure biome and subtype collections are allocated before assigning names
            while len(settings.biome_names) < 8:
                settings.biome_names.add()
            while len(settings.subtype_names) < 7:
                settings.subtype_names.add()

            for i, name in enumerate(s_data.get("biome_names", [])[:8]):
                settings.biome_names[i].name = name
            for i, name in enumerate(s_data.get("subtype_names", [])[:7]):
                settings.subtype_names[i].name = name

            settings.invert_r = s_data.get("invert_r", False)
            settings.invert_g = s_data.get("invert_g", False)
            settings.invert_b = s_data.get("invert_b", False)
            settings.detail_atlas_width = s_data.get("detail_atlas_width", 8)
            settings.detail_atlas_height = s_data.get("detail_atlas_height", 8)
            settings.noise_atlas_dimensions = s_data.get("noise_atlas_dimensions", 64)

            settings.detail_atlas_image = resolve_palette_image(
                s_data.get("detail_atlas_image_name", ""),
                s_data.get("detail_atlas_image_path", ""),
                get_default_detail_atlas,
                colorspace="Non-Color",
                alpha_mode='CHANNEL_PACKED',
            )
            settings.emission_pattern_image = resolve_palette_image(
                s_data.get("emission_pattern_image_name", ""),
                s_data.get("emission_pattern_image_path", ""),
                get_default_emission_pattern,
                colorspace="Non-Color",
                alpha_mode='CHANNEL_PACKED',
            )
            settings.iridescence_lut_image = resolve_palette_image(
                s_data.get("iridescence_lut_image_name", ""),
                s_data.get("iridescence_lut_image_path", ""),
                get_default_iridescence_lut,
                colorspace="sRGB",
            )

            # Rebuild schema slot definitions
            settings.slot_defs.clear()
            for sd in data.get("schema_definitions", []):
                new_sd = settings.slot_defs.add()
                for k, v in sd.items():
                    if v is not None and hasattr(new_sd, k):
                        try:
                            setattr(new_sd, k, v)
                        except (AttributeError, TypeError, ValueError):
                            pass

                if new_sd.data_type == 'ENUM':
                    options = [n.strip() for n in new_sd.enum_items.split(',') if n.strip()]
                    new_sd.min_val = 0.0
                    new_sd.max_val = float(max(0, len(options) - 1))

            # Rebuild palette materials
            scene.b2u_materials.clear()
            for md in data.get("materials", []):
                new_mat = scene.b2u_materials.add()

                new_mat.mat_name = md.get("mat_name", "Material")
                new_mat.workflow = md.get("workflow", "OPAQUE")
                new_mat.biome_type = md.get("biome_type", "INDEPENDENT")
                new_mat.start_row_index = md.get("start_row_index", 0)
                new_mat.allocated_rows = md.get("allocated_rows", 1)

                for dc in md.get("dynamic_colors", []):
                    new_dc = new_mat.dynamic_colors.add()
                    new_dc.emission = dc.get("emission", (0.0, 0.0, 0.0, 1.0))
                    new_dc.sec_emission = dc.get("sec_emission", (0.0, 0.0, 0.0, 1.0))
                    for i, val in enumerate(dc.get("bases", [])[:7]):
                        setattr(new_dc, f"base_{i}", val)
                    for i, val in enumerate(dc.get("depths", [])[:7]):
                        setattr(new_dc, f"depth_{i}", val)

                for vc in md.get("variant_colors", []):
                    new_vc = new_mat.variant_colors.add()
                    new_vc.emission = vc.get("emission", (0.0, 0.0, 0.0, 1.0))
                    new_vc.sec_emission = vc.get("sec_emission", (0.0, 0.0, 0.0, 1.0))
                    for i, val in enumerate(vc.get("bases", [])[:7]):
                        setattr(new_vc, f"base_{i}", val)
                    for i, val in enumerate(vc.get("depths", [])[:7]):
                        setattr(new_vc, f"depth_{i}", val)

                schema_type = "OPAQUE" if new_mat.workflow == "OPAQUE_DS" else new_mat.workflow
                for db in md.get("data_blocks", []):
                    new_db = new_mat.data_blocks.add()
                    new_db.name = db.get("name", "")
                    new_db.schema_type = db.get("schema_type") or schema_type

                    for key in ("f_val", "i_val", "b_val", "e_val"):
                        val = db.get(key)
                        if val is not None:
                            try:
                                setattr(new_db, key, str(val) if key == "e_val" else val)
                            except (AttributeError, TypeError, ValueError):
                                pass

                # Backfill any missing slot keys so all materials have a complete set of data blocks
                schema_type = "OPAQUE" if new_mat.workflow == "OPAQUE_DS" else new_mat.workflow
                existing_keys = {db.name for db in new_mat.data_blocks}
                for key in ALL_SLOT_KEYS:
                    if key not in existing_keys:
                        new_db = new_mat.data_blocks.add()
                        new_db.name = key
                        new_db.schema_type = schema_type

                new_mat.is_locked = md.get("is_locked", False)

        finally:
            settings.is_loading = False
            settings.is_dirty = False

        update_json_mtime(path)
        build_blender_preview_lut()
        push_schema_bounds_to_shaders(context)
        update_shader_preview_nodes(None, context)
        push_shader_uniforms(settings, context)

        settings.is_dirty = False

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {'PROPERTIES', 'VIEW_3D'}:
                    area.tag_redraw()

        return {'FINISHED'}


classes = (
    B2U_OT_reload_palette,
    B2U_OT_save_schema_json,
    B2U_OT_save_as_json,
    B2U_OT_load_schema_json,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)