# B2U Material Master (Blender Add-on)

A full pipeline add-on for authoring vertex data and LUT-driven materials for Unity URP.

> [!WARNING]
> **Beta Release:** B2U is still in active beta, so expect occasional bugs and rough edges. Please report issues or suggest features on the [Issue Tracker](https://github.com/CalebDNArt/B2U-Blender/issues).
> [!NOTE]
> **AI USE DISCLAIMER:** AI was used in the development of code for this package. As a solo developer, I would not have been able to build this package in less than a year without it. I wrote the majority of the code myself, with AI used for bug fixing and code review. **NO AI HAS BEEN USED FOR ANY IMAGE ASSETS.** All default images were created by hand in Blender or Photoshop. If you do not feel comfortable using the package due to AI usage, I absolutely respect your right to decline—please feel free to reference the source code if you would like to develop your own version. Thank you!

---

## Highlights

* **Shared JSON Palette & Schema Manager:** Author up to 256 different materials with support for dynamic biome blending (8 biomes, 1-7 subbiomes each) or 8 independent variants per material, customize per-workflow property schemas, lock finished slots, and stay synced with Unity via automatic disk polling and optional Auto-Save.
* **8-Bit PNG LUT Exporter:** Bake `ColorEmissionLUT` and `PropertyLUT` textures directly into your Unity project with normalized float, integer, boolean, enum, and bitmask channels.
* **Five Dedicated Mesh Workflows:** Automatic UV layout enforcement, vertex attribute initialization, and preview material management for **Opaque**, **Opaque Double Sided**, **Transparent**, **Crystal**, and **Liquid** objects.
* **Per-Face & Vertex Baking Tools:** Bake 16-bit packed spherical grain direction and scale, quad/tri sharp-edge distance fields and angle-based edge wear thickness, paint and emission masks, directional/curve/depth gradients, and raycasted thickness alpha.
* **Real-Time Fluid Flow Engine (`B2U_LiveFlow_Engine`):** Geometry Nodes modifier that computes curve-guided flow advection, obstacle deflection, collision distances, and circular biarc normal curvature live in the viewport, complete with 3D flow vector overlays.
* **Live Viewport Preview & Quick Edit:** Preview biome/subtype swaps, curvature, emission, and paint masks directly in Blender, and tweak active material properties from the 3D Viewport without switching workspaces.

---

## Requirements

* **Blender:** `5.1.2` or newer
* **Companion Unity Package:** [B2U Material Master for Unity](https://github.com/CalebDNArt/B2U-Unity) (Unity `6000.5.0f1`+ with URP Forward/Forward+)

---

## Installation

1. Download the latest `.zip` release from the [Releases](https://github.com/CalebDNArt/B2U-Blender/releases) page.
2. In Blender, open **Edit -> Preferences -> Get Extensions**.
3. Click the drop-down menu in the top-right corner and select **Install from Disk...** (or drag and drop the `.zip` file directly into Blender).
4. Select the downloaded `.zip` archive to install and enable **B2U Material Master**.

---

## Quick Start

1. Open **Edit -> Preferences -> Add-ons -> B2U Material Master** (or **Scene Properties -> B2U Palette Manager**) and set your **Unity Export Path** and **Palette Folder**.
2. Open the 3D Viewport N-Panel (`N`) -> **B2U Master** -> **Global Pipeline** and click **Create B2U Workspace**.
3. Create a new `.json` palette (using **B2U Default Setup**) or select an existing palette, add your materials, and click **Export LUTs to Unity**.
4. Select a mesh in the 3D Viewport, choose its **Object Workflow** in the **B2U Master** N-Panel, assign materials (`Ctrl + A` in Edit Mode), and bake any required UV or vertex data.
5. Prefix your exported objects (or root parent) with `B2U` and export to Unity with **Apply Modifiers**, UV maps, and Color Attributes enabled.

---

## Links

* [Documentation & Guides](https://dnart.ca/b2u/learn/)
* [Blender Setup Guide](https://dnart.ca/b2u/learn/blender/setup/)
* [B2U Material Master for Unity](https://github.com/CalebDNArt/B2U-Unity)
* [Report a Bug / Request a Feature](https://github.com/CalebDNArt/B2U-Blender/issues)

---

## License

This Blender add-on is licensed under the **GNU General Public License v3.0 or later (`GPL-3.0-or-later`)**. See the [LICENSE](./LICENSE) file for details.
