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
import re
import time
import bpy

B2U_LUT_UPDATE_QUEUED = False
B2U_AUTOSAVE_QUEUED = False
B2U_LAST_DIRTY_TIME = 0.0
B2U_LAST_JSON_MTIME = 0.0
B2U_LAST_BLENDER_SAVE_TIME = 0.0

_B2U_LAST_MAT_COUNT = -1
_B2U_LAST_IMG_COUNT = -1

B2U_SUFFIX_RE = re.compile(r"\.\d{3,}$")

# Maps internal shader blend image names and default filenames to their canonical datablock names
B2U_CANONICAL_IMAGES = {
    "B2U_Detail_Atlas_Default.png": "B2U_Detail_Atlas_Default.png",
    "B2U_Detail_Atlas_Default": "B2U_Detail_Atlas_Default.png",
    "Detail_Atlas_Default.png": "B2U_Detail_Atlas_Default.png",
    "Detail_Atlas_Default": "B2U_Detail_Atlas_Default.png",
    "B2U_Emission_Patterns_Default.png": "B2U_Emission_Patterns_Default.png",
    "B2U_Emission_Patterns_Default": "B2U_Emission_Patterns_Default.png",
    "Emission_Patterns_Default.png": "B2U_Emission_Patterns_Default.png",
    "Emission_Patterns_Default": "B2U_Emission_Patterns_Default.png",
    "B2U_Iridescence_LUT_Default.png": "B2U_Iridescence_LUT_Default.png",
    "B2U_Iridescence_LUT_Default": "B2U_Iridescence_LUT_Default.png",
    "Iridescence_LUT_Default.png": "B2U_Iridescence_LUT_Default.png",
    "Iridescence_LUT_Default": "B2U_Iridescence_LUT_Default.png",
    "B2U_Preview_ColorLUT": "B2U_Preview_ColorLUT",
    "B2U_Preview_PropLUT": "B2U_Preview_PropLUT",
}

B2U_MSGBUS_OWNER = object()

B2U_MASTER_MAT_MAPS = {
    'OPAQUE': 'B2U_Opaque_Preview',
    'OPAQUE_DS': 'B2U_Opaque_Preview',
    'TRANSPARENT': 'B2U_Transparent_Preview',
    'LIQUID': 'B2U_Liquid_Preview',
    'CRYSTAL': 'B2U_Crystal_Preview',
}

B2U_UNIQUE_PREVIEW_MATS = (
    'B2U_Opaque_Preview',
    'B2U_Transparent_Preview',
    'B2U_Liquid_Preview',
    'B2U_Crystal_Preview',
)

B2U_MASTER_TARGET_UVS = [
    "B2U_GrainAndScale",
    "B2U_EdgeIntensities",
    "B2U_EdgeDistances_XY",
    "B2U_EdgeDistances_ZW",
    "B2U_SmoothNormals_XY",
    "B2U_SmoothNormals_ZW",
]

# Default schema definitions:
# (schema, slot_id, ui_name, ui_description, data_type, min_val, max_val, default_val, is_active, items_str)
DEFAULT_SCHEMA = [
    # region OPAQUE SCHEMA
    ("OPAQUE", "r1_ca", "Metallic", "Metallic level of the surface", 'FLOAT', 0.0, 1.0, 0.0, True, ""),
    ("OPAQUE", "r1_ea", "Smoothness", "Smoothness of the surface", 'FLOAT', 0.0, 1.0, 0.5, True, ""),
    ("OPAQUE", "r1_p1_r", "Emission Strength", "Multiplier for the emission strength", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("OPAQUE", "r1_p1_g", "Emission Speed", "Multiplier for the speed of animated emission effects", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("OPAQUE", "r1_p1_b", "Iridescence Strength", "Strength of the iridescence effect", 'FLOAT', 0.0, 3.0, 1.0, True, ""),
    ("OPAQUE", "r1_p1_a", "Iridescence Power", "Changes how steep of an angle between the surface and view direction is needed to see iridescence", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("OPAQUE", "r1_p2_r", "Detail Gradient Power", "The power of the detail heightmap gradient", 'FLOAT', 0.0, 10.0, 1.0, True, ""),
    ("OPAQUE", "r1_p2_g", "Detail Normal Strength", "Strength of the detail normal texture", 'FLOAT', 0.0, 2.0, 0.1, True, ""),
    ("OPAQUE", "r1_p2_b", "Detail Curvature Strength", "Strength of the detail curvature effect", 'FLOAT', 0.0, 2.0, 1.0, True, ""),
    ("OPAQUE", "r1_p2_a", "Detail Booleans", "Controls several on/off features for detail mapping", 'BITMASK', 0.0, 255.0, 4.0, True, "Irregular Tiling, Inverted Normals, Inverted Curvature, Inverted Heightmap, Normals for Iridescence Only, Height-Based Emission, Curvature-Based Emission, Use Emission Color for Heightmap (Uses black if off)"),
    ("OPAQUE", "r1_p3_r", "Backface Color Multiplier", "Controls the tint of the backface color", 'FLOAT', -1.0, 1.0, 0.0, True, ""),
    ("OPAQUE", "r1_p3_g", "Backface Booleans", "Controls several on/off features for backface rendering", 'BITMASK', 0.0, 255.0, 0.0, True, "Invert Backface Normals, Invert Backface Curvature, Invert Backface Heightmap"),
    ("OPAQUE", "r1_p4_g", "Detail Index", "Index of the detail normal texture used for the material", 'ENUM', 0.0, 64.0, 0.0, True,
        "None, Wood Planks, Wood Beam, Rough Brick, Smooth Brick, Square Tile, Hexagonal Tile, Plaster, Fabric,"
        "Cobblestone, Concrete, Grate, Shingle, Thatch, Corrugated, Siding, Plastic,"
        "Wood Grain, Conifer Bark, Birch Bark, Roots, Moss, Gills, Honeycomb, Cellular,"
        "Bone, Brain, Muscle, Veins, Leather, Membrane, Scales, Segmented,"
        "Granite, Slate, Obsidian, Sharp Stone, Slag, Striated Stone, Coarse Sand, Fine Sand,"
        "Shards, Cracked Earth, Soil, Snow-Ice, Rusted Metal, Hammered Metal, Tarnished Metal, Cast Metal,"
        "Opal, Runes, Glyphs, Ornate Rings, Nested Hexagons, Star Chart, Bismuth, Warped,"
        "Molecular, Diamond Plate, Terraces, Vent, Circuitry, Riveted Plate, Bolted Plate, Coil"),
    ("OPAQUE", "r1_p4_b", "Emission Mode", "Mode of emission for the surface", 'ENUM', 0.0, 7.0, 0.0, True, "None, Solid, Breathing, Flickering, Pattern 1 (R), Pattern 2 (G), Pattern 3 (B), Pattern 4 (A)"),
    ("OPAQUE", "r1_p4_a", "Iridescence Mode", "Mode of iridescence for the surface", 'ENUM', 0.0, 8.0, 0.0, True, "None, Thin-Film, Rainbow, Structural Red, Structural Yellow, Structural Green, Structural Cyan, Structural Blue, Structural Magenta"),
    # endregion
    # region TRANSPARENT SCHEMA
    ("TRANSPARENT", "r1_ca", "Metallic", "Metallic level of the surface", 'FLOAT', 0.0, 1.0, 0.0, True, ""),
    ("TRANSPARENT", "r1_ea", "Smoothness", "Smoothness of the surface", 'FLOAT', 0.0, 1.0, 0.5, True, ""),
    ("TRANSPARENT", "r1_p1_r", "Emission Strength", "Multiplier for the emission strength", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("TRANSPARENT", "r1_p1_g", "Emission Speed", "Multiplier for the speed of animated emission effects", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("TRANSPARENT", "r1_p1_b", "Iridescence Strength", "Strength of the iridescence effect", 'FLOAT', 0.0, 3.0, 1.0, True, ""),
    ("TRANSPARENT", "r1_p1_a", "Iridescence Power", "Changes how steep of an angle between the surface and view direction is needed to see iridescence", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("TRANSPARENT", "r1_p2_r", "Detail Gradient Power", "The power of the detail heightmap gradient", 'FLOAT', 0.0, 10.0, 1.0, True, ""),
    ("TRANSPARENT", "r1_p2_g", "Detail Normal Strength", "Strength of the detail normal texture", 'FLOAT', 0.0, 2.0, 0.1, True, ""),
    ("TRANSPARENT", "r1_p2_b", "Detail Curvature Strength", "Strength of the detail curvature effect", 'FLOAT', 0.0, 2.0, 1.0, True, ""),
    ("TRANSPARENT", "r1_p2_a", "Detail Booleans", "Controls several on/off features for detail mapping", 'BITMASK', 0.0, 255.0, 4.0, True, "Irregular Tiling, Inverted Normals, Inverted Curvature, Inverted Heightmap, Normals for Iridescence Only, Height-Based Emission, Curvature-Based Emission, Use Emission Color for Heightmap (Uses black if off)"),
    ("TRANSPARENT", "r1_p4_g", "Detail Index", "Index of the detail normal texture used for the material", 'ENUM', 0.0, 64.0, 0.0, True,
        "None, Wood Planks, Wood Beam, Rough Brick, Smooth Brick, Square Tile, Hexagonal Tile, Plaster, Fabric,"
        "Cobblestone, Concrete, Grate, Shingle, Thatch, Corrugated, Siding, Plastic,"
        "Wood Grain, Conifer Bark, Birch Bark, Roots, Moss, Gills, Honeycomb, Cellular,"
        "Bone, Brain, Muscle, Veins, Leather, Membrane, Scales, Segmented,"
        "Granite, Slate, Obsidian, Sharp Stone, Slag, Striated Stone, Coarse Sand, Fine Sand,"
        "Shards, Cracked Earth, Soil, Snow-Ice, Rusted Metal, Hammered Metal, Tarnished Metal, Cast Metal,"
        "Opal, Runes, Glyphs, Ornate Rings, Nested Hexagons, Star Chart, Bismuth, Warped,"
        "Molecular, Diamond Plate, Terraces, Vent, Circuitry, Riveted Plate, Bolted Plate, Coil"),
    ("TRANSPARENT", "r1_p4_b", "Emission Mode", "Mode of emission for the surface", 'ENUM', 0.0, 7.0, 0.0, True, "None, Solid, Breathing, Flickering, Pattern 1 (R), Pattern 2 (G), Pattern 3 (B), Pattern 4 (A)"),
    ("TRANSPARENT", "r1_p4_a", "Iridescence Mode", "Mode of iridescence for the surface", 'ENUM', 0.0, 8.0, 0.0, True, "None, Thin-Film, Rainbow, Structural Red, Structural Yellow, Structural Green, Structural Cyan, Structural Blue, Structural Magenta"),
    # endregion
    # region CRYSTAL SCHEMA
    ("CRYSTAL", "r1_ca", "Metallic", "Metallic level of the surface", 'FLOAT', 0.0, 1.0, 0.0, True, ""),
    ("CRYSTAL", "r1_ea", "Smoothness", "Smoothness of the surface", 'FLOAT', 0.0, 1.0, 0.5, True, ""),
    ("CRYSTAL", "r1_p1_r", "Emission Strength", "Multiplier for the emission strength", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("CRYSTAL", "r1_p1_g", "Emission Speed", "Multiplier for the speed of animated emission effects", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("CRYSTAL", "r1_p1_b", "IOR", "The refractive index of the crystal", 'FLOAT', 1.0, 3.0, 1.5, True, ""),
    ("CRYSTAL", "r1_p1_a", "Transmission", "How much light passes through the crystal (Transparency)", 'FLOAT', 0.0, 1.0, 1.0, True, ""),
    ("CRYSTAL", "r1_p4_g", "Prismatic Shift", "Controls how much the prismatic colors shift from the surface color", 'FLOAT', 0.0, 1.0, 0.7, True, ""),
    ("CRYSTAL", "r1_p4_b", "Prismatic Power", "Changes how steep of an angle between the surface and view direction is needed to see the prismatic colors", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("CRYSTAL", "r1_p4_a", "Emission Mode", "Mode of emission for the surface", 'ENUM', 0.0, 7.0, 0.0, True, "None, Solid, Breathing, Flickering, Pattern 1 (R), Pattern 2 (G), Pattern 3 (B), Pattern 4 (A)"),
    # endregion
    # region LIQUID SCHEMA
    ("LIQUID", "r1_ca", "Metallic", "Metallic level of the surface", 'FLOAT', 0.0, 1.0, 0.0, True, ""),
    ("LIQUID", "r1_ea", "Smoothness", "Smoothness of the surface", 'FLOAT', 0.0, 1.0, 0.5, True, ""),
    ("LIQUID", "r1_p1_r", "Noise Scale", "Scale of the main noise", 'FLOAT', 0.0, 10.0, 1.0, True, ""),
    ("LIQUID", "r1_p1_g", "Noise Power", "Exponent to raise the value to (Controls the contrast between the surface and depth colors)", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("LIQUID", "r1_p1_b", "Distortion Scale", "Scale of the distortion noise", 'FLOAT', 0.0, 10.0, 1.0, True, ""),
    ("LIQUID", "r1_p1_a", "Distortion Strength", "Strength of the distortion noise", 'FLOAT', 0.0, 5.0, 0.1, True, ""),
    ("LIQUID", "r1_p2_r", "Phase Speed", "How quickly the shader flips between phase states (Increases flow speed but causes rapid shifting between phases)", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("LIQUID", "r1_p2_g", "Flow Strength", "Magnitude of the flow effect (Increases flow speed but causes noise bunching where slope changes)", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("LIQUID", "r1_p2_b", "Wave Height", "Height of the waves", 'FLOAT', 0.0, 2.0, 0.0, True, ""),
    ("LIQUID", "r1_p2_a", "Bubble Speed", "How much internal churning the liquid has", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("LIQUID", "r1_p3_r", "Emission Pattern Scale", "Scale of the emission pattern", 'FLOAT', 0.0, 10.0, 1.0, True, ""),
    ("LIQUID", "r1_p3_g", "Emission Strength", "Multiplier for the emission strength", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("LIQUID", "r1_p3_b", "Iridescence Strength", "Strength of the iridescence effect", 'FLOAT', 0.0, 3.0, 1.0, True, ""),
    ("LIQUID", "r1_p3_a", "Iridescence Power", "Changes how steep of an angle between the surface and view direction is needed to see iridescence", 'FLOAT', 0.0, 5.0, 1.0, True, ""),
    ("LIQUID", "r1_p4_b", "Emission Mode", "Mode of emission for the surface", 'ENUM', 0.0, 7.0, 0.0, True, "None, Solid, Breathing, Flickering, Pattern 1 (R), Pattern 2 (G), Pattern 3 (B), Pattern 4 (A)"),
    ("LIQUID", "r1_p4_a", "Iridescence Mode", "Mode of iridescence for the surface", 'ENUM', 0.0, 8.0, 0.0, True, "None, Thin-Film, Rainbow, Structural Red, Structural Yellow, Structural Green, Structural Cyan, Structural Blue, Structural Magenta"),
    ("LIQUID", "r2_ca", "Edge Distance", "Distance from the edge for the intersection", 'FLOAT', 0.0, 10.0, 1.0, True, ""),
    ("LIQUID", "r2_ea", "Viscosity", "A scalar value that controls how thick the liquid is. 0 has no drag, 1 is highly viscous Also controls the edge falloff", 'FLOAT', 0.0, 1.0, 0.02, True, ""),
    ("LIQUID", "r2_p1_r", "Transmission", "How much light transmits through the liquid", 'FLOAT', 0.0, 1.0, 0.5, True, ""),
    ("LIQUID", "r2_p1_g", "Absorption", "How many meters though the liquid light must travel to tint the background fully to the liquid color", 'FLOAT', 0.0, 5.0, 0.25, True, ""),
    ("LIQUID", "r2_p1_b", "IOR", "The refractive index of the liquid (1.33 for water)", 'FLOAT', 1.0, 2.0, 1.33, True, ""),
    # endregion
]

# Pre-indexed dictionary for O(1) default schema lookups
DEFAULT_SCHEMA_MAP = {(d[0], d[1]): d for d in DEFAULT_SCHEMA}

# All possible property slots across the two LUT rows
ALL_SLOT_KEYS = [
    "r1_ca", "r1_ea",
    "r1_p1_r", "r1_p1_g", "r1_p1_b", "r1_p1_a",
    "r1_p2_r", "r1_p2_g", "r1_p2_b", "r1_p2_a",
    "r1_p3_r", "r1_p3_g", "r1_p3_b", "r1_p3_a",
    "r1_p4_g", "r1_p4_b", "r1_p4_a",
    "r2_ca", "r2_ea",
    "r2_p1_r", "r2_p1_g", "r2_p1_b", "r2_p1_a",
    "r2_p2_r", "r2_p2_g", "r2_p2_b", "r2_p2_a",
    "r2_p3_r", "r2_p3_g", "r2_p3_b", "r2_p3_a",
    "r2_p4_r", "r2_p4_g", "r2_p4_b", "r2_p4_a",
]


# region Functions

def set_blender_save_time():
    """Records the timestamp when Blender saves a JSON palette to prevent self-triggered reload loops."""
    global B2U_LAST_BLENDER_SAVE_TIME
    B2U_LAST_BLENDER_SAVE_TIME = time.time()


def update_json_mtime(path=None):
    """Updates the cached modification timestamp of the active JSON palette file on disk."""
    global B2U_LAST_JSON_MTIME
    try:
        if path and os.path.exists(path):
            B2U_LAST_JSON_MTIME = os.path.getmtime(path)
        else:
            B2U_LAST_JSON_MTIME = 0.0
    except OSError:
        pass


def check_json_file_changes():
    """Timer callback that polls the active JSON palette on disk for external changes (e.g., from Unity)."""
    global B2U_LAST_JSON_MTIME, B2U_LAST_BLENDER_SAVE_TIME
    try:
        if not bpy.context or not getattr(bpy.context, "scene", None):
            return 2.0

        addon_name = __package__ or "b2u_material_master"
        if addon_name not in bpy.context.preferences.addons:
            return 2.0

        prefs = bpy.context.preferences.addons[addon_name].preferences
        settings = bpy.context.scene.b2u_settings

        active_file = settings.get("active_palette_file")
        active_file = str(active_file) if active_file is not None else "NONE"

        if active_file == "NONE" or not prefs.json_palette_dir:
            return 2.0

        dir_path = bpy.path.abspath(prefs.json_palette_dir)
        file_path = os.path.join(dir_path, active_file)

        if os.path.exists(file_path):
            current_mtime = os.path.getmtime(file_path)

            if B2U_LAST_JSON_MTIME == 0.0:
                B2U_LAST_JSON_MTIME = current_mtime
            elif current_mtime > B2U_LAST_JSON_MTIME:
                B2U_LAST_JSON_MTIME = current_mtime
                if time.time() - B2U_LAST_BLENDER_SAVE_TIME >= 3.0:
                    bpy.ops.b2u.load_schema_json()
                    print(f"B2U: Auto-Reloaded {active_file} from disk.")
    except Exception:
        pass
    return 2.0


def get_master_mat_map():
    """Returns a copy of the workflow-to-preview-material mapping dictionary."""
    return B2U_MASTER_MAT_MAPS.copy()


def check_autosave():
    """Timer callback that saves the active JSON palette after the configured inactivity period."""
    global B2U_AUTOSAVE_QUEUED, B2U_LAST_DIRTY_TIME
    try:
        settings = bpy.context.scene.b2u_settings

        if not settings.enable_autosave or settings.active_palette_file == "NONE" or not settings.is_dirty:
            B2U_AUTOSAVE_QUEUED = False
            return None

        time_since_dirty = time.time() - B2U_LAST_DIRTY_TIME

        if time_since_dirty >= settings.autosave_delay:
            bpy.ops.b2u.save_schema_json('EXEC_DEFAULT', export_luts=False)
            B2U_AUTOSAVE_QUEUED = False
            settings.last_autosave_string = time.strftime("%I:%M:%S %p", time.localtime())
            print("B2U: Palette Autosave Successful")
            return None
        return settings.autosave_delay - time_since_dirty
    except Exception:
        B2U_AUTOSAVE_QUEUED = False
        return None


def trigger_lut_rebuild():
    """Timer callback that rebuilds the internal preview LUTs and rebinds shader nodes."""
    global B2U_LUT_UPDATE_QUEUED
    B2U_LUT_UPDATE_QUEUED = False

    try:
        from .ops_meshtools import build_blender_preview_lut
        build_blender_preview_lut()
        update_shader_preview_nodes(bpy.context.scene.b2u_settings, bpy.context)

        if bpy.context.view_layer:
            bpy.context.view_layer.update()
    except Exception:
        pass
    return None


def mark_dirty(self, context):
    """Flags the palette as having unsaved changes and schedules debounced autosave and live LUT rebuilds."""
    global B2U_LAST_DIRTY_TIME, B2U_AUTOSAVE_QUEUED, B2U_LUT_UPDATE_QUEUED
    try:
        settings = context.scene.b2u_settings
        if not settings.is_loading:
            settings.is_dirty = True
            B2U_LAST_DIRTY_TIME = time.time()

            if settings.enable_autosave and not B2U_AUTOSAVE_QUEUED:
                B2U_AUTOSAVE_QUEUED = True
                bpy.app.timers.register(check_autosave, first_interval=settings.autosave_delay)

            if settings.enable_live_preview and not B2U_LUT_UPDATE_QUEUED:
                B2U_LUT_UPDATE_QUEUED = True
                bpy.app.timers.register(trigger_lut_rebuild, first_interval=0.15)
    except Exception:
        pass


def find_free_row_index(scene, needed_rows, ignore_ptr=0):
    """Finds the lowest contiguous block of unoccupied LUT rows of size `needed_rows`."""
    occupied = set()
    for m in scene.b2u_materials:
        if hasattr(m, 'as_pointer') and m.as_pointer() == ignore_ptr:
            continue
        start = m.start_row_index
        for i in range(m.allocated_rows):
            occupied.add(start + i)

    for i in range(256 - needed_rows + 1):
        if all((i + offset) not in occupied for offset in range(needed_rows)):
            return i
    return -1


def update_start_row(self, context):
    """Validates manual edits to a material's start row index and auto-relocates on collision."""
    try:
        settings = context.scene.b2u_settings
        if settings.is_loading:
            return

        val = self.start_row_index
        my_slots = set(range(val, val + self.allocated_rows))
        conflict = False

        if val < 0 or (val + self.allocated_rows - 1) > 255:
            conflict = True
        else:
            for m in context.scene.b2u_materials:
                if m.as_pointer() == self.as_pointer():
                    continue
                other_slots = set(range(m.start_row_index, m.start_row_index + m.allocated_rows))
                if my_slots.intersection(other_slots):
                    conflict = True
                    break

        if conflict:
            print(f"B2U WARNING: Row {val} is invalid or occupied! Auto-relocating...")
            new_idx = find_free_row_index(context.scene, self.allocated_rows, self.as_pointer())
            if new_idx != -1 and new_idx != val:
                self.start_row_index = new_idx
            return

        mark_dirty(self, context)
    except Exception:
        pass


def get_json_files(self, context):
    """Populates the EnumProperty list of available JSON palette files in the configured directory."""
    addon_name = __package__ or "b2u_material_master"
    items = [("NONE", "None (Clear Data)", "")]

    if addon_name in context.preferences.addons:
        prefs = context.preferences.addons[addon_name].preferences
        dir_path = bpy.path.abspath(prefs.json_palette_dir)
        if dir_path and os.path.exists(dir_path):
            for f in sorted(os.listdir(dir_path)):
                if f.endswith('.json'):
                    items.append((f, f[:-5], ""))

    raw_val = self.get("active_palette_file")
    if raw_val is not None:
        str_val = str(raw_val)
        if not any(i[0] == str_val for i in items):
            items.append((str_val, f"Missing Data ({str_val})", "Leftover scene data"))

    return items


def _load_default_addon_image(img_name, colorspace="Non-Color", alpha_mode=None):
    """Loads a default fallback texture from the add-on directory if not already present in bpy.data.images."""
    existing = bpy.data.images.get(img_name)
    if existing:
        return existing

    addon_dir = os.path.dirname(os.path.realpath(__file__))
    img_path = os.path.join(addon_dir, img_name)

    if os.path.exists(img_path):
        img = bpy.data.images.load(img_path, check_existing=True)
        img.name = img_name
        img.colorspace_settings.name = colorspace
        if alpha_mode:
            img.alpha_mode = alpha_mode
        return img

    print(f"B2U ERROR: {img_name} is missing from the add-on folder!")
    return None


def get_default_detail_atlas():
    """Returns or loads the default detail normal/curvature/height atlas texture."""
    return _load_default_addon_image("B2U_Detail_Atlas_Default.png", colorspace="Non-Color", alpha_mode='CHANNEL_PACKED')


def get_default_emission_pattern():
    """Returns or loads the default channel-packed emission pattern texture."""
    return _load_default_addon_image("B2U_Emission_Patterns_Default.png", colorspace="Non-Color", alpha_mode='CHANNEL_PACKED')


def get_default_iridescence_lut():
    """Returns or loads the default iridescence color LUT texture."""
    return _load_default_addon_image("B2U_Iridescence_LUT_Default.png", colorspace="sRGB")


def trigger_palette_load(self, context):
    """Loads the selected JSON palette or clears scene palette data when set to NONE."""
    if context.scene.b2u_settings.is_loading:
        return
    if context.scene.b2u_settings.active_palette_file == "NONE":
        bpy.ops.b2u.clear_palette_data()
    else:
        bpy.app.timers.register(lambda: bpy.ops.b2u.load_schema_json() and None, first_interval=0.01)


def toggle_live_preview(self, context):
    """Switches 3D Viewports between Material Preview and Solid Attribute shading when live preview is toggled."""
    try:
        settings = context.scene.b2u_settings
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            if settings.enable_live_preview:
                                space.shading.type = 'MATERIAL'
                            else:
                                space.shading.type = 'SOLID'
                                space.shading.color_type = 'ATTRIBUTE'

        if settings.enable_live_preview:
            from .ops_meshtools import build_blender_preview_lut
            build_blender_preview_lut()
            update_shader_preview_nodes(self, context)
    except Exception:
        pass


def update_allocation(self, context):
    """Updates row allocation and resets schema data blocks when a material's workflow or biome type changes."""
    try:
        if context.scene.b2u_settings.is_loading:
            return
    except Exception:
        pass

    if self.workflow == 'LIQUID' and self.biome_type == 'INDEP_DYN':
        self.biome_type = 'INDEPENDENT'

    needed_rows = 2 if (self.workflow == 'LIQUID' or self.biome_type == 'INDEP_DYN') else 1

    if needed_rows > self.allocated_rows:
        current_start = self.start_row_index
        occupied = set()
        for m in context.scene.b2u_materials:
            if m.as_pointer() == self.as_pointer():
                continue
            start = m.start_row_index
            for i in range(m.allocated_rows):
                occupied.add(start + i)

        my_new_slots = set(range(current_start, current_start + needed_rows))
        if my_new_slots.intersection(occupied) or (current_start + needed_rows - 1 > 255):
            new_idx = find_free_row_index(context.scene, needed_rows, self.as_pointer())
            if new_idx != -1:
                self.start_row_index = new_idx
            else:
                print("B2U WARNING: Not enough contiguous space for this workflow expansion!")

    self.allocated_rows = needed_rows

    target_schema = 'OPAQUE' if self.workflow == 'OPAQUE_DS' else self.workflow
    existing_keys = {db.name for db in self.data_blocks}
    for key in ALL_SLOT_KEYS:
        if key not in existing_keys:
            b = self.data_blocks.add()
            b.name = key
            b.schema_type = target_schema

    slot_lookup = {
        s.slot_id: s
        for s in context.scene.b2u_settings.slot_defs
        if s.schema == target_schema
    }

    for block in getattr(self, "data_blocks", []):
        block.schema_type = target_schema
        s_def = slot_lookup.get(block.name)
        if s_def:
            if s_def.data_type == 'FLOAT':
                block.f_val = s_def.default_val
            elif s_def.data_type in {'INT', 'BITMASK'}:
                block.i_val = int(s_def.default_val)
            elif s_def.data_type == 'BOOL':
                block.b_val = s_def.default_b_val
            elif s_def.data_type == 'ENUM':
                block.e_val = str(int(s_def.default_val))

    mark_dirty(self, context)


def get_or_load_preview_material(mat_name):
    """Retrieves a B2U preview material from bpy.data or appends it from b2u_shaders.blend."""
    if mat_name in bpy.data.materials:
        return bpy.data.materials[mat_name]

    addon_dir = os.path.dirname(os.path.realpath(__file__))
    blend_path = os.path.join(addon_dir, "b2u_shaders.blend")

    if not os.path.exists(blend_path):
        print(f"B2U WARNING: Shader file not found at {blend_path}")
        return None

    try:
        with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
            data_to.materials = [
                m for m in B2U_UNIQUE_PREVIEW_MATS
                if m in data_from.materials and m not in bpy.data.materials
            ]

        cleanup_b2u_duplicates()
        return bpy.data.materials.get(mat_name)
    except Exception as e:
        print(f"B2U ERROR: Failed to load material {mat_name} - {e}")
    return None


def push_shader_uniforms(self, context):
    """Pushes global preview settings and auxiliary texture references into the Blender preview shader node trees."""
    try:
        settings = context.scene.b2u_settings
        if not settings.enable_live_preview:
            return

        values = {
            "Preview_Biome": float(settings.preview_biome),
            "Preview_Subtype": float(settings.preview_subtype),
            "Curvature_Strength": float(settings.preview_curvature_strength),
            "Curvature_Power": float(settings.preview_curvature_power),
            "Emission_Mult": float(settings.preview_emission_strength),
            "Emission_Speed": float(settings.preview_emission_speed),
            "Detail_Tex_Width": 8.0, #float(settings.detail_atlas_width),
            "Detail_Tex_Height": 8.0, #float(settings.detail_atlas_height),
            "Noise_Scale": float(settings.preview_noise_scale),
            "Noise_Strength": float(settings.preview_noise_strength),
            "Triplanar_Blend_Power": float(settings.preview_triplanar_blend_power),
            "Invert_R": 1.0 if settings.invert_r else 0.0,
            "Invert_G": 1.0 if settings.invert_g else 0.0,
            "Invert_B": 1.0 if settings.invert_b else 0.0,
            "Show_Paint_Mask": 1.0 if settings.preview_show_paint else 0.0,
        }

        images = {
            "B2U_Detail_Atlas": settings.detail_atlas_image,
            "B2U_Emission_Pattern": settings.emission_pattern_image,
            "B2U_Iridescence_LUT": settings.iridescence_lut_image,
        }

        visited_trees = set()

        def update_tree_safe(nodes):
            for node in nodes:
                base_name = node.name.split('.')[0]

                if base_name in values:
                    if node.outputs[0].default_value != values[base_name]:
                        node.outputs[0].default_value = values[base_name]
                elif base_name in images:
                    img = images[base_name]
                    if img and getattr(node, "image", None) != img:
                        node.image = img

                if node.type == 'GROUP' and node.node_tree:
                    if node.node_tree.name not in visited_trees:
                        visited_trees.add(node.node_tree.name)
                        update_tree_safe(node.node_tree.nodes)

        for mat_name in B2U_UNIQUE_PREVIEW_MATS:
            mat = bpy.data.materials.get(mat_name)
            if mat and mat.use_nodes:
                update_tree_safe(mat.node_tree.nodes)

    except Exception as e:
        print(f"B2U Uniform Push Error: {e}")


def push_schema_bounds_to_shaders(context):
    """Pushes per-slot Min/Max ranges and Enum option counts into the Blender preview shader node trees."""
    try:
        settings = context.scene.b2u_settings

        bounds_dict = {}
        for s_def in settings.slot_defs:
            prefix = f"{'OPAQUE' if s_def.schema == 'OPAQUE_DS' else s_def.schema}_{s_def.slot_id}"
            bounds_dict[f"{prefix}_Min"] = s_def.min_val
            bounds_dict[f"{prefix}_Max"] = s_def.max_val
            if s_def.data_type == 'ENUM':
                options = [n for n in s_def.enum_items.split(',') if n.strip()]
                bounds_dict[f"{prefix}_Options"] = float(max(1, len(options)))

        visited_trees = set()

        def update_bounds_safe(nodes):
            for node in nodes:
                base_name = node.name.split('.')[0]
                if base_name in bounds_dict:
                    if node.outputs[0].default_value != bounds_dict[base_name]:
                        node.outputs[0].default_value = bounds_dict[base_name]

                if node.type == 'GROUP' and node.node_tree:
                    if node.node_tree.name not in visited_trees:
                        visited_trees.add(node.node_tree.name)
                        update_bounds_safe(node.node_tree.nodes)

        for mat_name in B2U_UNIQUE_PREVIEW_MATS:
            mat = bpy.data.materials.get(mat_name)
            if mat and mat.use_nodes:
                update_bounds_safe(mat.node_tree.nodes)

    except Exception as e:
        print(f"B2U Schema Bounds Push Error: {e}")

def cleanup_b2u_duplicates(context=None):
    """Remaps and purges duplicate (.001+) B2U preview materials, node groups, and auxiliary images."""
    global _B2U_LAST_MAT_COUNT, _B2U_LAST_IMG_COUNT

    ctx = context or bpy.context
    scene = getattr(ctx, "scene", None)

    # 1. Ensure default auxiliary images exist and scene pointers reference the canonical datablocks
    if scene and hasattr(scene, "b2u_settings"):
        settings = scene.b2u_settings
        if not settings.detail_atlas_image or B2U_SUFFIX_RE.search(settings.detail_atlas_image.name):
            settings.detail_atlas_image = get_default_detail_atlas()
        if not settings.emission_pattern_image or B2U_SUFFIX_RE.search(settings.emission_pattern_image.name):
            settings.emission_pattern_image = get_default_emission_pattern()
        if not settings.iridescence_lut_image or B2U_SUFFIX_RE.search(settings.iridescence_lut_image.name):
            settings.iridescence_lut_image = get_default_iridescence_lut()

    # 2. Deduplicate B2U preview materials (e.g., B2U_Opaque_Preview.001 -> B2U_Opaque_Preview)
    for mat in list(bpy.data.materials):
        base_name = B2U_SUFFIX_RE.sub("", mat.name)
        if base_name in B2U_UNIQUE_PREVIEW_MATS and mat.name != base_name:
            canon_mat = bpy.data.materials.get(base_name)
            if canon_mat:
                mat.user_remap(canon_mat)
                bpy.data.materials.remove(mat)
            else:
                mat.name = base_name

    # 3. Deduplicate B2U node groups (Shader & Geometry Node trees duplicated during paste)
    for ng in list(bpy.data.node_groups):
        base_name = B2U_SUFFIX_RE.sub("", ng.name)
        if base_name.startswith("B2U_") and ng.name != base_name:
            canon_ng = bpy.data.node_groups.get(base_name)
            if canon_ng:
                ng.user_remap(canon_ng)
                bpy.data.node_groups.remove(ng)
            else:
                ng.name = base_name

    # 4. Deduplicate B2U auxiliary and preview LUT images
    for img in list(bpy.data.images):
        stripped_name = B2U_SUFFIX_RE.sub("", img.name)
        target_name = B2U_CANONICAL_IMAGES.get(
            stripped_name,
            stripped_name if stripped_name.startswith("B2U_") else None,
        )
        if not target_name:
            continue

        canon_img = bpy.data.images.get(target_name)
        if canon_img and img != canon_img:
            img.user_remap(canon_img)
            bpy.data.images.remove(img)
        elif not canon_img and img.name != target_name:
            img.name = target_name

    _B2U_LAST_MAT_COUNT = len(bpy.data.materials)
    _B2U_LAST_IMG_COUNT = len(bpy.data.images)


def enforce_master_materials(context):
    """Ensures all B2U mesh objects in the scene reference the shared master preview material for their workflow."""
    for obj in context.scene.objects:
        if obj.type == 'MESH':
            wf = getattr(obj, 'b2u_workflow', 'NONE')
            if wf in B2U_MASTER_MAT_MAPS:
                target_name = B2U_MASTER_MAT_MAPS[wf]
                master_mat = bpy.data.materials.get(target_name)
                if master_mat:
                    is_correct = len(obj.data.materials) == 1 and obj.data.materials[0] == master_mat
                    if not is_correct:
                        obj.data.materials.clear()
                        obj.data.materials.append(master_mat)

    cleanup_b2u_duplicates(context)


def silent_material_reunify():
    """Timer callback that reassigns the shared master preview material to pasted objects and purges duplicates."""
    try:
        if bpy.context and getattr(bpy.context, "scene", None):
            enforce_master_materials(bpy.context)
    except Exception:
        pass
    return None


def on_active_object_change(*args):
    """Msgbus callback triggered when the active object changes in the scene."""
    if not bpy.app.timers.is_registered(silent_material_reunify):
        bpy.app.timers.register(silent_material_reunify, first_interval=0.05)


def update_shader_preview_nodes(self, context):
    """Updates the LUT texture references and LUT width constant in the preview shader node trees."""
    try:
        settings = context.scene.b2u_settings
        if not settings.enable_live_preview:
            return

        enforce_master_materials(context)

        lut_width = float((8 * settings.num_subtypes) + 8)
        color_img = bpy.data.images.get("B2U_Preview_ColorLUT")
        prop_img = bpy.data.images.get("B2U_Preview_PropLUT")

        visited_trees = set()

        def update_lut_tree(nodes):
            for node in nodes:
                base_name = node.name.split('.')[0]
                if base_name == "LUT_Width":
                    if node.outputs[0].default_value != lut_width:
                        node.outputs[0].default_value = lut_width
                elif base_name == "B2U_Preview_ColorLUT" and color_img:
                    if getattr(node, "image", None) != color_img:
                        node.image = color_img
                elif base_name == "B2U_Preview_PropLUT" and prop_img:
                    if getattr(node, "image", None) != prop_img:
                        node.image = prop_img

                if node.type == 'GROUP' and node.node_tree:
                    if node.node_tree.name not in visited_trees:
                        visited_trees.add(node.node_tree.name)
                        update_lut_tree(node.node_tree.nodes)

        for mat_name in B2U_UNIQUE_PREVIEW_MATS:
            mat = bpy.data.materials.get(mat_name)
            if mat and mat.use_nodes:
                update_lut_tree(mat.node_tree.nodes)

    except Exception as e:
        print(f"B2U Shader Update Error: {e}")


def ensure_uv_maps(mesh, wf_type):
    """Enforces the exact UV map layout and ordering required by the specified workflow."""
    if wf_type not in {'CRYSTAL', 'LIQUID'}:
        local_types = B2U_MASTER_TARGET_UVS
    elif wf_type == 'CRYSTAL':
        local_types = ["B2U_GrainAndScale"]
    else:
        local_types = ["B2U_LiquidFlow", "B2U_LiquidCurvature", "B2U_LiquidData"]

    for uv_layer in list(mesh.uv_layers):
        if uv_layer.name not in local_types:
            mesh.uv_layers.remove(uv_layer)

    current_names = [uv.name for uv in mesh.uv_layers]
    if current_names != local_types:
        uv_data_cache = {
            name: [tuple(d.uv) for d in mesh.uv_layers[name].data]
            for name in current_names
        }

        while mesh.uv_layers:
            mesh.uv_layers.remove(mesh.uv_layers[0])

        for name in local_types:
            new_layer = mesh.uv_layers.new(name=name)
            if name in uv_data_cache:
                for d, saved_uv in zip(new_layer.data, uv_data_cache[name]):
                    d.uv = saved_uv
            elif name in {"B2U_EdgeDistances_XY", "B2U_EdgeDistances_ZW"}:
                for d in new_layer.data:
                    d.uv = (9999.0, 9999.0)


def update_obj_workflow(self, context):
    """Initializes or cleans up mesh attributes, UV layers, modifiers, and preview materials when an object's workflow changes."""
    mesh = self.data
    if not mesh or not isinstance(mesh, bpy.types.Mesh):
        return

    wf = self.b2u_workflow

    if wf in {'LIQUID', 'NONE'}:
        for m_name in ("B2U_EdgeWear_Bevel", "B2U_EdgeWear"):
            mod = self.modifiers.get(m_name)
            if mod:
                self.modifiers.remove(mod)

    if wf != 'LIQUID':
        mod = self.modifiers.get("B2U_LiveFlow_Engine")
        if mod:
            self.modifiers.remove(mod)
        if "B2U_Tangent" in mesh.attributes:
            mesh.attributes.remove(mesh.attributes["B2U_Tangent"])
        if "B2U_Bitangent" in mesh.attributes:
            mesh.attributes.remove(mesh.attributes["B2U_Bitangent"])

    if wf == 'NONE':
        return

    ensure_uv_maps(mesh, wf)

    target_mat_name = B2U_MASTER_MAT_MAPS.get(wf)
    if target_mat_name:
        preview_mat = get_or_load_preview_material(target_mat_name)
        if preview_mat:
            mesh.materials.clear()
            mesh.materials.append(preview_mat)

    layer = mesh.color_attributes.get("B2U_VertexData")
    if wf in {'LIQUID', 'NONE'}:
        if layer:
            mesh.color_attributes.remove(layer)
            layer = None
    else:
        if not layer:
            layer = mesh.color_attributes.new(name="B2U_VertexData", type='BYTE_COLOR', domain='CORNER')
            mesh.color_attributes.active_color = layer

    old_liquid_color = mesh.color_attributes.get("B2U_LiquidData")
    if old_liquid_color:
        mesh.color_attributes.remove(old_liquid_color)

    valid_rows = set()
    for m in context.scene.b2u_materials:
        if m.workflow == wf:
            valid_rows.add(m.start_row_index)
            if m.allocated_rows == 2:
                valid_rows.add(m.start_row_index + 1)

    fallback_row = min(valid_rows) if valid_rows else 0

    if layer:
        for d in layer.data:
            current_row = int(round(d.color[3] * 255.0))
            new_row = current_row if current_row in valid_rows else fallback_row
            d.color = (0.0, 0.0, 0.0, new_row / 255.0)

    elif wf == 'LIQUID':
        data_uv = mesh.uv_layers.get("B2U_LiquidData")
        if data_uv:
            for d in data_uv.data:
                dist = d.uv[0] if d.uv[0] != 0.0 else 9999.0
                current_packed = int(round(d.uv[1]))
                current_row = current_packed & 255
                upper_flags = current_packed & ~255
                new_row = current_row if current_row in valid_rows else fallback_row
                d.uv = (dist, float(upper_flags | new_row))

    mesh.update()


def get_workflow_items(self, context):
    """Returns valid workflow options for a material slot, requiring 2 contiguous free rows for Liquids."""
    items = [
        ('OPAQUE', "Opaque", "Standard opaque hard surfaces"),
        ('OPAQUE_DS', "Opaque Double Sided", "Foliage, cloth, or unculled surfaces"),
        ('TRANSPARENT', "Transparent", "Glass, ice, magical/sci-fi barriers"),
        ('CRYSTAL', "Crystal", "Complex refraction and transmission"),
    ]
    my_ptr = self.as_pointer() if hasattr(self, 'as_pointer') else 0
    if find_free_row_index(context.scene, 2, my_ptr) != -1:
        items.append(('LIQUID', "Liquid", "Water, lava, acid (Allocates 2 rows)"))
    return items


def get_biome_response_items(self, context):
    """Returns valid biome response modes for a material slot."""
    items = [
        ('INDEPENDENT', "Independent", "Global colors"),
        ('DYNAMIC', "Dynamic", "Biome-specific colors"),
    ]
    if getattr(self, 'workflow', None) == 'LIQUID':
        return items

    my_ptr = self.as_pointer() if hasattr(self, 'as_pointer') else 0
    if find_free_row_index(context.scene, 2, my_ptr) != -1:
        items.append(('INDEP_DYN', "Indep & Dynamic", "Global base, dynamic details"))
    return items


def get_preview_biome_items(self, context):
    """Returns the 8 biome names for the viewport preview dropdown."""
    settings = context.scene.b2u_settings
    return [
        (str(i), settings.biome_names[i].name, "")
        if i < len(settings.biome_names) else (str(i), f"Biome {i}", "")
        for i in range(8)
    ]


def get_preview_subtype_items(self, context):
    """Returns the active subtype names for the viewport preview dropdown."""
    settings = context.scene.b2u_settings
    return [
        (str(i), settings.subtype_names[i].name, "")
        if i < len(settings.subtype_names) else (str(i), f"Subtype {i}", "")
        for i in range(max(1, settings.num_subtypes))
    ]


def check_schema_compliance(self, context):
    """Verifies whether the active schema definitions match the standard B2U default schema."""
    try:
        settings = context.scene.b2u_settings
        if settings.is_loading:
            return

        is_default = True
        for s_def in settings.slot_defs:
            match = DEFAULT_SCHEMA_MAP.get((s_def.schema, s_def.slot_id))
            expected_active = bool(match[8]) if match else False

            if s_def.is_active != expected_active:
                is_default = False
                break

            if expected_active:
                if s_def.ui_name != match[2] or s_def.data_type != match[4]:
                    is_default = False
                    break

        settings.palette_uses_default_schema = is_default
        mark_dirty(self, context)
    except Exception:
        pass


def defer_schema_update(self, context):
    """Schedules a deferred clamping pass and shader bounds update when a schema slot definition is modified."""
    try:
        if context.scene.b2u_settings.is_loading:
            return
    except Exception:
        pass

    schema_name = self.schema
    slot_id = self.slot_id

    def apply_clamping():
        try:
            if not bpy.context or not bpy.context.scene:
                return 0.1
            settings = bpy.context.scene.b2u_settings

            s_def = next((s for s in settings.slot_defs if s.schema == schema_name and s.slot_id == slot_id), None)
            if not s_def:
                return None

            was_loading = settings.is_loading
            settings.is_loading = True
            try:
                if s_def.data_type == 'INT':
                    r_min = float(round(s_def.min_val))
                    r_max = float(round(s_def.max_val))
                    r_def = float(round(s_def.default_val))
                    if s_def.min_val != r_min:
                        s_def.min_val = r_min
                    if s_def.max_val != r_max:
                        s_def.max_val = r_max
                    if s_def.default_val != r_def:
                        s_def.default_val = r_def
                elif s_def.data_type == 'ENUM':
                    options = [n.strip() for n in s_def.enum_items.split(',') if n.strip()]
                    max_idx = float(max(0, len(options) - 1))
                    r_def = float(min(max(int(round(s_def.default_val)), 0), int(max_idx)))
                    if s_def.min_val != 0.0:
                        s_def.min_val = 0.0
                    if s_def.max_val != max_idx:
                        s_def.max_val = max_idx
                    if s_def.default_val != r_def:
                        s_def.default_val = r_def
            finally:
                settings.is_loading = was_loading

            if hasattr(bpy.context.scene, 'b2u_materials'):
                for mat in bpy.context.scene.b2u_materials:
                    if mat.workflow == schema_name or (schema_name == 'OPAQUE' and mat.workflow == 'OPAQUE_DS'):
                        block = mat.data_blocks.get(slot_id)
                        if block:
                            if s_def.data_type == 'FLOAT':
                                cur = block.get("_f_val", s_def.default_val)
                                block["_f_val"] = min(max(cur, s_def.min_val), s_def.max_val)
                            elif s_def.data_type == 'INT':
                                cur = block.get("_i_val", int(s_def.default_val))
                                block["_i_val"] = int(min(max(cur, s_def.min_val), s_def.max_val))
                            elif s_def.data_type == 'BITMASK':
                                cur = block.get("_i_val", int(s_def.default_val))
                                block["_i_val"] = int(min(max(cur, 0), 255))
                            elif s_def.data_type == 'ENUM':
                                try:
                                    cur_idx = int(block.e_val)
                                except (ValueError, TypeError):
                                    cur_idx = int(s_def.default_val)
                                clamped_idx = min(max(cur_idx, 0), int(s_def.max_val))
                                if str(clamped_idx) != block.e_val:
                                    block.e_val = str(clamped_idx)

            push_schema_bounds_to_shaders(bpy.context)

            wm = getattr(bpy.context, "window_manager", None)
            if wm and hasattr(wm, "windows"):
                for window in wm.windows:
                    if hasattr(window, "screen") and window.screen:
                        for area in window.screen.areas:
                            if area.type == 'PROPERTIES':
                                area.tag_redraw()
        except ReferenceError:
            pass
        return None

    bpy.app.timers.register(apply_clamping, first_interval=0.01)
    check_schema_compliance(self, context)


def dynamic_enum_items(self, context):
    """Generates EnumProperty items dynamically from the active schema slot's comma-separated string."""
    try:
        lookup = 'OPAQUE' if self.schema_type == 'OPAQUE_DS' else self.schema_type
        s_def = next(
            (s for s in context.scene.b2u_settings.slot_defs if s.schema == lookup and s.slot_id == self.name),
            None,
        )
        if s_def and s_def.data_type == 'ENUM':
            items = [
                (str(i), name.strip(), "")
                for i, name in enumerate(s_def.enum_items.split(','))
                if name.strip()
            ]
            if items:
                return items
    except Exception:
        pass
    return [("0", "None", "")]


# region Data Block Get/Set

def update_e_val(self, context):
    try:
        if not context.scene.b2u_settings.is_loading:
            mark_dirty(self, context)
    except Exception:
        pass


def update_b_val(self, context):
    try:
        if not context.scene.b2u_settings.is_loading:
            mark_dirty(self, context)
    except Exception:
        pass


def get_f_val(self):
    return self.get("_f_val", 0.0)


def set_f_val(self, value):
    try:
        settings = bpy.context.scene.b2u_settings
        lookup = 'OPAQUE' if self.schema_type == 'OPAQUE_DS' else self.schema_type
        s_def = next((s for s in settings.slot_defs if s.schema == lookup and s.slot_id == self.name), None)
        if s_def and s_def.data_type == 'FLOAT':
            self["_f_val"] = min(max(value, s_def.min_val), s_def.max_val)
        else:
            self["_f_val"] = value
        if not settings.is_loading:
            mark_dirty(self, bpy.context)
    except Exception:
        self["_f_val"] = value


def get_i_val(self):
    return self.get("_i_val", 0)


def set_i_val(self, value):
    try:
        settings = bpy.context.scene.b2u_settings
        lookup = 'OPAQUE' if self.schema_type == 'OPAQUE_DS' else self.schema_type
        s_def = next((s for s in settings.slot_defs if s.schema == lookup and s.slot_id == self.name), None)
        if s_def and s_def.data_type == 'INT':
            self["_i_val"] = int(min(max(value, s_def.min_val), s_def.max_val))
        elif s_def and s_def.data_type == 'BITMASK':
            self["_i_val"] = int(min(max(value, 0), 255))
        else:
            self["_i_val"] = value
        if not settings.is_loading:
            mark_dirty(self, bpy.context)
    except Exception:
        self["_i_val"] = value

# endregion


def update_flow_handler(self, context):
    """Toggles the 3D Viewport GPU draw handler for fluid flow vectors."""
    try:
        from .ops_meshtools import manage_flow_handler
        manage_flow_handler(self.flow_show_vectors)
    except Exception:
        pass


def update_grad_handler(self, context):
    """Toggles the 3D Viewport GPU draw handler for custom gradient and grain vectors."""
    try:
        from .ops_meshtools import manage_grad_handler
        manage_grad_handler(self.grad_show_custom)
    except Exception:
        pass


def update_liq_tessellated(self, context):
    """Mutually excludes Volumetric liquid mode when Tessellated mode is enabled on an Object."""
    if self.b2u_liq_tessellated:
        self.b2u_liq_volumetric = False


def update_liq_volumetric(self, context):
    """Mutually excludes Tessellated liquid mode when Volumetric mode is enabled on an Object."""
    if self.b2u_liq_volumetric:
        self.b2u_liq_tessellated = False

# endregion


# region Classes

class B2UNamingSlot(bpy.types.PropertyGroup):
    """Stores a single biome or subtype name string."""
    name: bpy.props.StringProperty(default="New Name", update=mark_dirty)


class B2USlotDef(bpy.types.PropertyGroup):
    """Defines the metadata, type, and clamping bounds for a single LUT schema slot."""
    schema: bpy.props.StringProperty()
    slot_id: bpy.props.StringProperty()
    ui_name: bpy.props.StringProperty(update=check_schema_compliance)
    ui_description: bpy.props.StringProperty(update=mark_dirty)

    data_type: bpy.props.EnumProperty(
        name="Data Type",
        items=[
            ('FLOAT', "Float", ""),
            ('INT', "Integer", ""),
            ('BOOL', "Boolean", ""),
            ('ENUM', "Enum", ""),
            ('BITMASK', "Bitmask", ""),
        ],
        default='FLOAT',
        update=defer_schema_update,
    )
    enum_items: bpy.props.StringProperty(name="Enum Elements", default="Option 1, Option 2, Option 3", update=defer_schema_update)
    bitmask_items: bpy.props.StringProperty(name="Bitmask Elements", default="Flag 1, Flag 2, Flag 3", update=mark_dirty)

    min_val: bpy.props.FloatProperty(default=0.0, update=defer_schema_update)
    max_val: bpy.props.FloatProperty(default=1.0, update=defer_schema_update)
    default_val: bpy.props.FloatProperty(default=0.0, update=defer_schema_update)
    default_b_val: bpy.props.BoolProperty(default=False, description="Default State", update=defer_schema_update)
    is_active: bpy.props.BoolProperty(default=False, update=check_schema_compliance)


class B2USettings(bpy.types.PropertyGroup):
    """Scene-level B2U palette settings, viewport preview controls, and mesh tool parameters."""
    is_dirty: bpy.props.BoolProperty(default=False)
    is_loading: bpy.props.BoolProperty(default=False)
    active_palette_file: bpy.props.EnumProperty(
        name="Active Palette",
        items=get_json_files,
        update=trigger_palette_load,
        description="The active JSON Palette file.",
    )

    enable_autosave: bpy.props.BoolProperty(
        name="Auto-Save JSON",
        default=False,
        description="Automatically save the palette data after a brief period of inactivity (Does not auto-export LUTs)",
    )
    autosave_delay: bpy.props.FloatProperty(
        name="Auto-Save Delay",
        default=10.0,
        min=0.5,
        max=30.0,
        description="Seconds of inactivity before the palette automatically saves",
    )
    last_autosave_string: bpy.props.StringProperty(default="Never")

    ui_show_global: bpy.props.BoolProperty(default=True)
    ui_show_preview: bpy.props.BoolProperty(default=True)
    ui_show_assigner: bpy.props.BoolProperty(default=True)
    ui_show_uv_tools: bpy.props.BoolProperty(default=True)
    ui_show_workflow: bpy.props.BoolProperty(default=True)
    ui_show_paint: bpy.props.BoolProperty(default=False)
    ui_show_edgewear: bpy.props.BoolProperty(default=False)
    ui_show_emission: bpy.props.BoolProperty(default=False)
    ui_show_gradient: bpy.props.BoolProperty(default=False)
    ui_show_alpha: bpy.props.BoolProperty(default=False)
    ui_show_flow: bpy.props.BoolProperty(default=False)

    enable_live_preview: bpy.props.BoolProperty(name="Enable Live Shader Preview", default=False, update=toggle_live_preview)
    palette_uses_default_schema: bpy.props.BoolProperty(name="Palette Uses Default Schema", default=True)

    preview_biome: bpy.props.EnumProperty(name="Preview Biome", items=get_preview_biome_items, update=push_shader_uniforms)
    preview_subtype: bpy.props.EnumProperty(name="Preview Subtype", items=get_preview_subtype_items, update=push_shader_uniforms)
    preview_variant: bpy.props.EnumProperty(
        name="Edit Variant",
        items=[(str(i), f"Variant {i + 1}", "") for i in range(8)],
        update=push_shader_uniforms,
    )

    preview_curvature_strength: bpy.props.FloatProperty(name="Curvature Strength (Global)", default=0.5, min=-5.0, max=5.0, update=push_shader_uniforms)
    preview_curvature_power: bpy.props.FloatProperty(name="Curvature Power (Global)", default=1.0, min=0.1, max=5.0, update=push_shader_uniforms)
    preview_emission_strength: bpy.props.FloatProperty(name="Emission Multiplier (Global)", default=1.0, min=0.0, max=5.0, update=push_shader_uniforms)
    preview_emission_speed: bpy.props.FloatProperty(name="Emission Speed (Global)", default=1.0, min=0.0, max=10.0, update=push_shader_uniforms)
    preview_noise_strength: bpy.props.FloatProperty(name="Noise Strength (Global)", default=0.1, min=0.0, max=1.0, update=push_shader_uniforms)
    preview_noise_scale: bpy.props.FloatProperty(name="Noise Scale (Global)", default=1.0, min=0.1, max=10.0, update=push_shader_uniforms)
    preview_triplanar_blend_power: bpy.props.FloatProperty(name="Triplanar Blend Power (Global)", default=4.0, min=0.1, max=20.0, update=push_shader_uniforms)
    preview_show_paint: bpy.props.BoolProperty(name="Show Paint Mask", default=False, update=push_shader_uniforms)

    def get_assigner_mats(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or obj.b2u_workflow == 'NONE':
            return [("-1", "None", "")]
        wf = obj.b2u_workflow

        valid_mats = sorted(
            [(i, m) for i, m in enumerate(context.scene.b2u_materials) if m.workflow == wf],
            key=lambda x: x[1].start_row_index,
        )

        items = []
        for i, m in valid_mats:
            id_text = (
                f"ID: {m.start_row_index}-{m.start_row_index + 1}"
                if m.workflow == 'LIQUID' or m.biome_type == 'INDEP_DYN'
                else f"ID: {m.start_row_index}"
            )
            items.append((str(i), f"{id_text}: {m.mat_name}", f"Row ID: {m.start_row_index}"))

        return items if items else [("-1", "No Valid Materials", "")]

    active_assigner_mat_id: bpy.props.EnumProperty(name="Material", items=get_assigner_mats)
    assigner_biome_response: bpy.props.EnumProperty(
        name="Response",
        items=[('INDEPENDENT', "Independent (Uniform)", ""), ('DYNAMIC', "Dynamic (Biome)", "")],
    )
    assigner_variant: bpy.props.EnumProperty(
        name="Color Variant",
        items=[(str(i), f"Variant {i + 1}", "") for i in range(8)],
    )

    assigner_emission_val: bpy.props.FloatProperty(name="Emission Strength", default=1.0, min=0.0, max=1.0)

    grad_axis: bpy.props.EnumProperty(
        name="Gradient Application",
        items=[
            ('X', "X Axis", ""),
            ('Y', "Y Axis", ""),
            ('Z', "Z Axis", ""),
            ('CUSTOM AXIS', "Custom Axis", ""),
            ('DEPTH', "Depth", ""),
            ('CURVE', "Curve", ""),
        ],
        default='Z',
    )

    grad_bounds_min: bpy.props.FloatProperty(name="Gradient Start", default=-1.0)
    grad_bounds_max: bpy.props.FloatProperty(name="Gradient End", default=1.0)

    grad_curve: bpy.props.PointerProperty(type=bpy.types.Object, name="Target Curve", poll=lambda self, obj: obj.type == 'CURVE')
    grad_curve_min: bpy.props.FloatProperty(name="Start Eval", default=0.0, min=0.0, max=1.0)
    grad_curve_max: bpy.props.FloatProperty(name="End Eval", default=1.0, min=0.0, max=1.0)

    grad_custom_start: bpy.props.FloatVectorProperty(name="Custom Start", default=(0.0, 0.0, 0.0))
    grad_custom_end: bpy.props.FloatVectorProperty(name="Custom End", default=(0.0, 0.0, 1.0))
    grad_reverse: bpy.props.BoolProperty(name="Reverse Direction", default=False)

    grad_show_custom: bpy.props.BoolProperty(name="Show Vector", default=False, update=update_grad_handler)

    assigner_alpha_val: bpy.props.FloatProperty(name="Alpha Strength", default=0.0, min=0.0, max=1.0)
    alpha_bake_max_depth: bpy.props.FloatProperty(name="Max Depth Target", default=1.0, min=0.001)
    alpha_bake_reverse: bpy.props.BoolProperty(name="Reverse Direction", default=False)
    alpha_bake_axis: bpy.props.EnumProperty(
        name="Projection Axis",
        items=[
            ('NORMAL', "Inward Normals (Organic)", ""),
            ('X', "X Axis", ""),
            ('Y', "Y Axis", ""),
            ('Z', "Z Axis", ""),
        ],
        default='NORMAL',
    )

    auto_bake_edgewear: bpy.props.BoolProperty(name="Auto-Bake on Edit", default=True)
    edge_wear_angle: bpy.props.FloatProperty(name="Angle Threshold", default=0.523599, subtype='ANGLE')
    edge_wear_thickness: bpy.props.FloatProperty(name="Global Thickness", default=1.0, min=0.0, max=2.0)

    assigner_paint_val: bpy.props.FloatProperty(name="Paint Strength", default=1.0, min=0.0, max=1.0)

    num_subtypes: bpy.props.IntProperty(name="Active Subtypes", default=3, min=1, max=7, update=mark_dirty)
    show_naming_ui: bpy.props.BoolProperty(name="Show Core Settings", default=False)
    show_schema_ui: bpy.props.BoolProperty(name="Show Schema Configurator", default=False)
    schema_view_mode: bpy.props.EnumProperty(
        items=[
            ('OPAQUE', "Opaque", ""),
            ('TRANSPARENT', "Transparent", ""),
            ('LIQUID', "Liquid", ""),
            ('CRYSTAL', "Crystal", ""),
        ]
    )

    biome_names: bpy.props.CollectionProperty(type=B2UNamingSlot)
    subtype_names: bpy.props.CollectionProperty(type=B2UNamingSlot)
    slot_defs: bpy.props.CollectionProperty(type=B2USlotDef)

    uv_bake_rotation: bpy.props.FloatProperty(name="Detail Orientation", min=0.0, max=360.0, default=0.0)
    uv_bake_scale: bpy.props.FloatVectorProperty(name="Projection Scale", size=2, min=0.0, max=5.0, default=(1.0, 1.0))
    uv_bake_offset: bpy.props.FloatVectorProperty(name="Projection Offset", size=2, min=-10.0, max=10.0, default=(0.0, 0.0))

    is_flow_painting: bpy.props.BoolProperty(default=False)

    def update_flow_inversions(self, context):
        if self.is_loading:
            return
        mark_dirty(self, context)
        original_active = context.active_object
        original_selected = context.selected_objects.copy()

        for obj in context.scene.objects:
            if obj.type == 'MESH' and getattr(obj, 'b2u_workflow', 'NONE') == 'LIQUID':
                if obj.modifiers.get("B2U_LiveFlow_Engine"):
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    context.view_layer.objects.active = obj
                    try:
                        bpy.ops.b2u.add_live_flow_modifier()
                    except Exception as e:
                        print(f"B2U Flow Update Error on {obj.name}: {e}")

        bpy.ops.object.select_all(action='DESELECT')
        for obj in original_selected:
            try:
                obj.select_set(True)
            except Exception:
                pass
        if original_active:
            context.view_layer.objects.active = original_active

        try:
            push_shader_uniforms(self, context)
        except Exception:
            pass
        if context.view_layer:
            context.view_layer.update()

    invert_r: bpy.props.BoolProperty(name="Invert Flow X", default=False, update=update_flow_inversions)
    invert_g: bpy.props.BoolProperty(name="Invert Flow Y", default=False, update=update_flow_inversions)
    invert_b: bpy.props.BoolProperty(name="Invert B (Strength)", default=False, update=update_flow_inversions)

    def update_global_images(self, context):
        try:
            settings = context.scene.b2u_settings
            for img in (settings.detail_atlas_image, settings.emission_pattern_image):
                if img:
                    img.colorspace_settings.name = "Non-Color"
                    img.alpha_mode = 'CHANNEL_PACKED'
            if settings.iridescence_lut_image:
                settings.iridescence_lut_image.colorspace_settings.name = "sRGB"
            push_shader_uniforms(self, context)
        except Exception:
            pass

    detail_atlas_image: bpy.props.PointerProperty(type=bpy.types.Image, name="Detail Atlas", update=update_global_images)
    detail_atlas_width: bpy.props.IntProperty(name="Atlas Columns", default=8, min=1, update=update_global_images)
    detail_atlas_height: bpy.props.IntProperty(name="Atlas Rows", default=8, min=1, update=update_global_images)
    noise_atlas_dimensions: bpy.props.IntProperty(name="Noise Atlas Dimensions", default=64, min=1)

    emission_pattern_image: bpy.props.PointerProperty(type=bpy.types.Image, name="Emission Pattern", update=update_global_images)
    iridescence_lut_image: bpy.props.PointerProperty(type=bpy.types.Image, name="Iridescence LUT", update=update_global_images)

    flow_target_curve: bpy.props.PointerProperty(type=bpy.types.Object, name="Flow Curve", poll=lambda self, obj: obj.type == 'CURVE')
    flow_collision_collection: bpy.props.PointerProperty(type=bpy.types.Collection, name="Collision Objects")
    flow_show_vectors: bpy.props.BoolProperty(name="Show Vectors", default=False, update=update_flow_handler)
    flow_vector_length: bpy.props.FloatProperty(name="Vector Length", default=0.5, min=0.001, max=10.0)
    flow_vector_color: bpy.props.FloatVectorProperty(name="Vector Color", subtype='COLOR', default=(1.0, 0.8, 0.0), min=0.0, max=1.0, size=3)

    grain_axis: bpy.props.EnumProperty(
        name="Direction",
        items=[('X', "X Axis", ""), ('Y', "Y Axis", ""), ('Z', "Z Axis", ""), ('CUSTOM', "Custom Vector", "")],
        default='Z',
    )
    grain_reverse: bpy.props.BoolProperty(name="Reverse Direction", default=False)
    grain_scale: bpy.props.FloatVectorProperty(name="Texture Scale", size=2, default=(1.0, 1.0))


class B2UDynamicColorData(bpy.types.PropertyGroup):
    """Stores base, depth, and emission colors for a single biome or variant column block."""
    emission: bpy.props.FloatVectorProperty(name="Emission", subtype='COLOR', size=4, default=(0.0, 0.0, 0.0, 1.0), min=0.0, max=1.0, update=mark_dirty)
    base_0: bpy.props.FloatVectorProperty(name="Base 0", subtype='COLOR', size=4, default=(0.5, 0.5, 0.5, 1.0), min=0.0, max=1.0, update=mark_dirty)
    base_1: bpy.props.FloatVectorProperty(name="Base 1", subtype='COLOR', size=4, default=(0.5, 0.5, 0.5, 1.0), min=0.0, max=1.0, update=mark_dirty)
    base_2: bpy.props.FloatVectorProperty(name="Base 2", subtype='COLOR', size=4, default=(0.5, 0.5, 0.5, 1.0), min=0.0, max=1.0, update=mark_dirty)
    base_3: bpy.props.FloatVectorProperty(name="Base 3", subtype='COLOR', size=4, default=(0.5, 0.5, 0.5, 1.0), min=0.0, max=1.0, update=mark_dirty)
    base_4: bpy.props.FloatVectorProperty(name="Base 4", subtype='COLOR', size=4, default=(0.5, 0.5, 0.5, 1.0), min=0.0, max=1.0, update=mark_dirty)
    base_5: bpy.props.FloatVectorProperty(name="Base 5", subtype='COLOR', size=4, default=(0.5, 0.5, 0.5, 1.0), min=0.0, max=1.0, update=mark_dirty)
    base_6: bpy.props.FloatVectorProperty(name="Base 6", subtype='COLOR', size=4, default=(0.5, 0.5, 0.5, 1.0), min=0.0, max=1.0, update=mark_dirty)

    sec_emission: bpy.props.FloatVectorProperty(name="Secondary Emission", subtype='COLOR', size=4, default=(0.0, 0.0, 0.0, 1.0), min=0.0, max=1.0, update=mark_dirty)
    depth_0: bpy.props.FloatVectorProperty(name="Depth 0", subtype='COLOR', size=4, default=(0.1, 0.1, 0.1, 1.0), min=0.0, max=1.0, update=mark_dirty)
    depth_1: bpy.props.FloatVectorProperty(name="Depth 1", subtype='COLOR', size=4, default=(0.1, 0.1, 0.1, 1.0), min=0.0, max=1.0, update=mark_dirty)
    depth_2: bpy.props.FloatVectorProperty(name="Depth 2", subtype='COLOR', size=4, default=(0.1, 0.1, 0.1, 1.0), min=0.0, max=1.0, update=mark_dirty)
    depth_3: bpy.props.FloatVectorProperty(name="Depth 3", subtype='COLOR', size=4, default=(0.1, 0.1, 0.1, 1.0), min=0.0, max=1.0, update=mark_dirty)
    depth_4: bpy.props.FloatVectorProperty(name="Depth 4", subtype='COLOR', size=4, default=(0.1, 0.1, 0.1, 1.0), min=0.0, max=1.0, update=mark_dirty)
    depth_5: bpy.props.FloatVectorProperty(name="Depth 5", subtype='COLOR', size=4, default=(0.1, 0.1, 0.1, 1.0), min=0.0, max=1.0, update=mark_dirty)
    depth_6: bpy.props.FloatVectorProperty(name="Depth 6", subtype='COLOR', size=4, default=(0.1, 0.1, 0.1, 1.0), min=0.0, max=1.0, update=mark_dirty)


class B2UDataBlock(bpy.types.PropertyGroup):
    """Stores the value of a single LUT property channel for a material."""
    name: bpy.props.StringProperty()
    schema_type: bpy.props.StringProperty()
    f_val: bpy.props.FloatProperty(get=get_f_val, set=set_f_val)
    i_val: bpy.props.IntProperty(get=get_i_val, set=set_i_val)
    b_val: bpy.props.BoolProperty(update=update_b_val)
    e_val: bpy.props.EnumProperty(items=dynamic_enum_items, update=update_e_val)


class B2UMaterialSlot(bpy.types.PropertyGroup):
    """Stores all configuration, color blocks, and property channels for a single palette material."""
    is_locked: bpy.props.BoolProperty(name="Lock Material", default=False)
    mat_name: bpy.props.StringProperty(name="Name", default="New Material", update=mark_dirty)
    workflow: bpy.props.EnumProperty(name="Workflow", items=get_workflow_items, update=update_allocation)
    biome_type: bpy.props.EnumProperty(name="Biome Response", items=get_biome_response_items, update=update_allocation)
    start_row_index: bpy.props.IntProperty(default=0, min=0, max=255, update=update_start_row)
    allocated_rows: bpy.props.IntProperty(default=1)

    variant_colors: bpy.props.CollectionProperty(type=B2UDynamicColorData)
    dynamic_colors: bpy.props.CollectionProperty(type=B2UDynamicColorData)
    data_blocks: bpy.props.CollectionProperty(type=B2UDataBlock)

# endregion


def initialize_b2u_settings(scene):
    """Populates default biome names, subtype names, schema slots, and fallback textures on the scene."""
    settings = scene.b2u_settings
    while len(settings.biome_names) < 8:
        idx = len(settings.biome_names)
        settings.biome_names.add().name = f"Biome {idx}"
    while len(settings.subtype_names) < 7:
        idx = len(settings.subtype_names)
        settings.subtype_names.add().name = f"Subtype {idx}"

    if len(settings.slot_defs) == 0:
        for schema in ("OPAQUE", "TRANSPARENT", "LIQUID", "CRYSTAL"):
            for key in ALL_SLOT_KEYS:
                new_def = settings.slot_defs.add()
                new_def.schema = schema
                new_def.slot_id = key
                match = DEFAULT_SCHEMA_MAP.get((schema, key))
                if match:
                    new_def.ui_name = match[2]
                    new_def.ui_description = match[3]
                    new_def.data_type = match[4]
                    new_def.min_val = match[5]
                    new_def.max_val = match[6]
                    new_def.default_val = match[7]
                    new_def.default_b_val = bool(match[7]) if match[4] == 'BOOL' else False
                    new_def.is_active = match[8]
                    new_def.enum_items = match[9]
                    new_def.bitmask_items = match[9]
                else:
                    new_def.ui_name = f"Custom ({key})"
                    new_def.ui_description = "Add description"
                    new_def.is_active = False

    for mat in scene.b2u_materials:
        existing_keys = {db.name for db in mat.data_blocks}
        schema_type = 'OPAQUE' if mat.workflow == 'OPAQUE_DS' else mat.workflow
        for key in ALL_SLOT_KEYS:
            if key not in existing_keys:
                b = mat.data_blocks.add()
                b.name = key
                b.schema_type = schema_type

    if not settings.detail_atlas_image:
        settings.detail_atlas_image = get_default_detail_atlas()
    if not settings.emission_pattern_image:
        settings.emission_pattern_image = get_default_emission_pattern()
    if not settings.iridescence_lut_image:
        settings.iridescence_lut_image = get_default_iridescence_lut()

@bpy.app.handlers.persistent
def b2u_depsgraph_handler(scene, depsgraph):
    """Detects newly pasted or appended materials/images in O(1) time and schedules deduplication."""
    global _B2U_LAST_MAT_COUNT, _B2U_LAST_IMG_COUNT
    mat_count = len(bpy.data.materials)
    img_count = len(bpy.data.images)

    if mat_count != _B2U_LAST_MAT_COUNT or img_count != _B2U_LAST_IMG_COUNT:
        _B2U_LAST_MAT_COUNT = mat_count
        _B2U_LAST_IMG_COUNT = img_count
        if not bpy.app.timers.is_registered(silent_material_reunify):
            bpy.app.timers.register(silent_material_reunify, first_interval=0.05)

@bpy.app.handlers.persistent
def b2u_load_handler(dummy=None):
    """Initializes B2U palette data, preview materials, and file-watch timers when a .blend file is loaded."""
    def delayed_init():
        if not bpy.context or not getattr(bpy.context, "scene", None):
            return 0.1

        addon_name = __package__ or "b2u_material_master"
        loaded_from_json = False

        if addon_name in bpy.context.preferences.addons:
            prefs = bpy.context.preferences.addons[addon_name].preferences
            dir_path = bpy.path.abspath(prefs.json_palette_dir)
            file_name = bpy.context.scene.b2u_settings.active_palette_file

            if dir_path and os.path.exists(dir_path) and file_name != "NONE":
                if os.path.exists(os.path.join(dir_path, file_name)):
                    try:
                        bpy.ops.b2u.load_schema_json()
                        loaded_from_json = True
                    except Exception as e:
                        print(f"B2U Auto-Load Failed: {e}")

        if not loaded_from_json:
            initialize_b2u_settings(bpy.context.scene)

        for obj in bpy.context.scene.objects:
            if obj.type == 'MESH' and getattr(obj, 'b2u_workflow', 'NONE') != 'NONE':
                if obj.b2u_workflow in B2U_MASTER_MAT_MAPS:
                    preview_mat = get_or_load_preview_material(B2U_MASTER_MAT_MAPS[obj.b2u_workflow])
                    if preview_mat:
                        obj.data.materials.clear()
                        obj.data.materials.append(preview_mat)

        cleanup_b2u_duplicates(bpy.context)
        return None

    bpy.app.timers.register(delayed_init, first_interval=0.1)

    global B2U_LAST_JSON_MTIME
    B2U_LAST_JSON_MTIME = 0.0
    if not bpy.app.timers.is_registered(check_json_file_changes):
        bpy.app.timers.register(check_json_file_changes, first_interval=2.0)


# region Registration

classes = (
    B2UNamingSlot,
    B2USlotDef,
    B2UDataBlock,
    B2USettings,
    B2UDynamicColorData,
    B2UMaterialSlot,
)

OBJECT_LIQUID_PROPS = (
    "b2u_liq_tessellated",
    "b2u_liq_volumetric",
    "b2u_liq_displacement",
    "b2u_liq_transmissive",
    "b2u_liq_ignore_gravity",
    "b2u_liq_caustics",
    "b2u_liq_invert_height",
    "b2u_liq_height_transmission",
    "b2u_liq_height_emission",
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Object.b2u_workflow = bpy.props.EnumProperty(
        name="Object Workflow",
        items=[
            ('NONE', "Unassigned", "Initialize object to a workflow"),
            ('OPAQUE', "Opaque", ""),
            ('OPAQUE_DS', "Opaque Double Sided", ""),
            ('TRANSPARENT', "Transparent", ""),
            ('LIQUID', "Liquid", ""),
            ('CRYSTAL', "Crystal", ""),
        ],
        default='NONE',
        update=update_obj_workflow,
    )

    bpy.types.Object.b2u_liq_tessellated = bpy.props.BoolProperty(name="Tessellated", update=update_liq_tessellated)
    bpy.types.Object.b2u_liq_volumetric = bpy.props.BoolProperty(name="Volumetric", update=update_liq_volumetric)
    bpy.types.Object.b2u_liq_displacement = bpy.props.BoolProperty(name="Use Displacement")
    bpy.types.Object.b2u_liq_transmissive = bpy.props.BoolProperty(name="Is Transmissive")
    bpy.types.Object.b2u_liq_ignore_gravity = bpy.props.BoolProperty(name="Ignore Gravity")
    bpy.types.Object.b2u_liq_caustics = bpy.props.BoolProperty(name="Has Caustics")
    bpy.types.Object.b2u_liq_invert_height = bpy.props.BoolProperty(name="Invert Height")
    bpy.types.Object.b2u_liq_height_transmission = bpy.props.BoolProperty(name="Height Based Transmission")
    bpy.types.Object.b2u_liq_height_emission = bpy.props.BoolProperty(name="Height Based Emission")

    bpy.types.Scene.b2u_settings = bpy.props.PointerProperty(type=B2USettings)
    bpy.types.Scene.b2u_materials = bpy.props.CollectionProperty(type=B2UMaterialSlot)
    bpy.types.Scene.b2u_active_mat_index = bpy.props.IntProperty(name="Active Material Index", default=0)

    if b2u_load_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(b2u_load_handler)
    b2u_load_handler()

    if b2u_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(b2u_depsgraph_handler)

    bpy.msgbus.clear_by_owner(B2U_MSGBUS_OWNER)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.LayerObjects, "active"),
        owner=B2U_MSGBUS_OWNER,
        args=(),
        notify=on_active_object_change,
    )


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.b2u_settings
    del bpy.types.Scene.b2u_materials
    del bpy.types.Scene.b2u_active_mat_index
    del bpy.types.Object.b2u_workflow

    for prop_name in OBJECT_LIQUID_PROPS:
        if hasattr(bpy.types.Object, prop_name):
            delattr(bpy.types.Object, prop_name)

    if b2u_load_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(b2u_load_handler)

    for t in (check_json_file_changes, check_autosave, trigger_lut_rebuild, silent_material_reunify):
        if bpy.app.timers.is_registered(t):
            bpy.app.timers.unregister(t)

    if b2u_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(b2u_depsgraph_handler)

    for mat in list(bpy.data.materials):
        if B2U_SUFFIX_RE.sub("", mat.name) in B2U_UNIQUE_PREVIEW_MATS:
            bpy.data.materials.remove(mat)

    for group in list(bpy.data.node_groups):
        if B2U_SUFFIX_RE.sub("", group.name).startswith("B2U_"):
            bpy.data.node_groups.remove(group, do_unlink=True)

    for img in list(bpy.data.images):
        stripped = B2U_SUFFIX_RE.sub("", img.name)
        if stripped in B2U_CANONICAL_IMAGES or stripped.startswith("B2U_Preview_"):
            bpy.data.images.remove(img)

    for group in list(bpy.data.node_groups):
        if isinstance(group, bpy.types.ShaderNodeTree) and group.name.startswith("B2U_"):
            bpy.data.node_groups.remove(group, do_unlink=True)

    bpy.msgbus.clear_by_owner(B2U_MSGBUS_OWNER)

# endregion