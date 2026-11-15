# Texture Plugger

A Blender add-on (Extension format, Blender 4.5 and 5.0+) that hooks PBR texture sets up to a
Principled BSDF material. It detects each map from its filename and wires everything up
(colour spaces, normal/bump/displacement, packed maps). Run it again and you get the same
result, never duplicated nodes. Pure Python with no dependencies. It makes no network calls.

![A stone floor and two painted metal crates, textured in one go by Texture Plugger](docs/images/render_after.jpg)

| Before | After loading two texture folders |
|---|---|
| ![The same scene with no materials](docs/images/render_before.jpg) | ![The scene after Texture Plugger built both materials](docs/images/render_after.jpg) |

## What it looks like

**1. A folder of maps.** Texture Plugger reads each filename and works out which map it is.
Both naming styles below are detected without any setup: Poly Haven style suffixes
(`_diff`, `_nor_gl`, `_disp`...) and Unreal style ones, including a packed ORM map and a
DirectX normal map.

![Two texture sets and the map type detected for each file](docs/images/texture_folder.png)

**2. The panel.** Press **Load Texture Set** and pick the folder (or drag the images in).
Each detected map gets a row you can switch off, re-type or swap. The folder holds two
sets, so the *Set* menu lets you choose; the one with the most maps is picked first.

![The Texture Plugger sidebar panel in the 3D Viewport](docs/images/panel.png)

**3. The node tree it builds.** Everything lands in one labelled frame with a fixed
layout. The stone set gets AO multiplied into Base Color, an OpenGL normal map and real
displacement:

![Node tree built for the stone_tiles set](docs/images/nodes_stone.png)

The metal set's DirectX normal map has its green channel flipped, and the packed ORM map is
split into AO, Roughness and Metallic:

![Node tree built for the T_PaintedMetal set](docs/images/nodes_metal.png)

The example textures were generated procedurally just for these screenshots. The renders
are Cycles; the panel screenshot is Blender 5.0's Material Preview.

## Install

1. Build the zip with `.\build.ps1` (or `python build.py`). This creates `dist\texture_plugger-1.3.0.zip`.
2. In Blender go to **Edit > Preferences > Get Extensions**, open the **v** menu (top right),
   choose **Install from Disk...** and pick the zip.
3. Check that **Texture Plugger** is enabled.

The same zip also installs as a legacy add-on (**Add-ons > Install from Disk**).

## Use

- **Sidebar** (`N`) > **Texture Plugger** tab, in the 3D Viewport and in the Shader Editor.
- **Load Texture Set**: you can pick several image files, or open a folder and press the
  button without selecting anything to scan the whole folder. You can also use
  **File > Import > PBR Texture Set**.
- **Drag and drop** image files onto the 3D Viewport or Shader Editor. Blender's own image
  drop handlers (e.g. *Add Image Empty* in the 3D Viewport, *Image node* in the Shader
  Editor) also accept images, so Blender usually shows a small menu: choose
  **Texture Plugger: PBR texture set**. The textures go to the active object (or the selection,
  see *Apply To*), not to the object under the mouse.
- The **slot list** shows each detected map. In each row you can:
  - turn the slot on or off
  - change the map type
  - **browse** for a different file
  - **clear** the file

  Below the list you can add/remove slots and re-detect. Press **Apply** to (re)build.
  Files the add-on doesn't recognise show up as *Unassigned*.
- If the folder holds **several texture sets**, the panel shows a *Set* dropdown. The set
  with the most maps is picked first.
- **Remove Textures** deletes only the nodes the add-on created. It also restores the
  material settings it changed. Your own nodes are never touched.
- **Apply To** decides what gets textured: the active object, or all selected objects.
  A material shared by several objects is only built once.
- **Options** (collapsed sub-panel):
  - **Material**: use the active slot, or make a new material named after the set
  - **Apply After Loading**
  - **Projection**: UV or Box/triplanar (with blend), plus texture scale
  - **AO multiply** on/off and AO strength
  - **Normal strength**
  - **Height map**: Displacement, Bump or Not connected
  - **Displacement**: scale (0.1) and midlevel (0.5)
  - **Bump**: strength and distance
  - **Transparency**: Dithered or Blended
  - **Base Color Alpha as Opacity**
  - **Emission strength**
  - **Relative paths**

## Create & Paint (Substance 3D Painter)

Use this when you want to paint the textures yourself in Adobe Substance 3D Painter and
see the result in Blender. You need Painter installed. The add-on looks for it in the
usual places; if it can't find it, set the path in the add-on preferences.

1. Select your object. Open the **Create & Paint** sub-panel in the sidebar.
2. Pick a **Folder**, tick the **maps** you want (Base Color, Normal, Metallic and
   Roughness are on by default; Emission, Alpha and Displacement are optional) and choose
   the placeholder **Size**.
3. Press **Create Texture Set**. The name is the object's material name (or the *Name* you
   typed). If the object has no material, one is created.
4. Press **Edit in Substance Painter**. Painter starts with your mesh and an export folder
   that is already set.
5. Paint. When you want to see it in Blender, in Painter use **File > Export Textures**,
   with the output template **Blender (Principled BSDF)** and the format **PNG**. Blender
   updates the material a moment after the export finishes.

Pressing **Ctrl+S** in Painter does not update Blender. It only saves the Painter project
(a `.spp` file), not the images. Blender changes only when you export.

Create Texture Set makes this layout inside the folder you picked:

```
<Folder>/
  Crate/                      one folder per texture set, named after the material
    textures/
      Crate_BaseColor.png     flat colour placeholders, plugged into the material
      Crate_Normal.png
      Crate_Metallic.png
      Crate_Roughness.png
      (Crate_Emission.png, Crate_Alpha.png, Crate_Displacement.png if ticked)
    mesh/
      Crate.fbx               your object, exported for Painter
```

The file names are exactly the ones the "Blender (Principled BSDF)" export writes, so each
export replaces a placeholder. Base Color is set up as sRGB and all the other maps as
Non-Color. Because the add-on writes the textures, the material is wired up straight away.

- **Save your project inside the set folder** (for example `Crate/Crate.spp`). The next time
  you press **Edit in Substance Painter**, it reopens that project instead of starting a new
  one. If there are several `.spp` files there, the newest one is used.
- **Existing files are never overwritten.** Pressing Create Texture Set again only adds
  missing files, and your painted textures stay as they are.
- **Auto Reload** is on by default. It checks the texture files about once a second and
  reloads any that changed on disk. Images with unsaved paint in Blender are never reloaded.
  You can switch it off and change the interval, and set the **Painter path**, in
  **Edit > Preferences > Add-ons > Texture Plugger**. The **Reload Textures** button in
  the panel reloads everything right away. **Open Folder** shows the set folder.
- **Works with Brushstroke Tools.** Brushstrokes are separate objects with their own
  material, so they render on top of the Painter textures. When a brushstrokes object is
  active, the Create & Paint buttons act on the mesh the strokes sit on. To colour the
  strokes from the Painter texture, pick the set's Base Color image as the brush
  material's colour texture; it then updates on every Painter export too.
- **Undo can't break the link.** If you press Ctrl+Z past the point where the set was
  created, the add-on puts the material, its textures and the folder link straight back
  (the files on disk were never touched). To really disconnect a material from its set
  folder, press **Unlink Texture Set** in the panel; it keeps the nodes and the files. Use
  **Remove Textures** to take the nodes out.
- **One texture set per material.** The buttons always work on the active material of the
  active object. If an object has several materials, create one set per material by
  switching the active material slot. A second material with the same name gets
  `_2` (so `Crate_2`), because Painter names the set after the material.
- Don't rename the material after creating the set: Painter would export files with the new
  name and the placeholders would not be replaced (the add-on warns you).

### A whole scene at once

1. Select the objects you want to paint (box-select in the viewport or the Outliner).
   Objects inside groups (parented to empties) and nested collections are included.
2. Press **Create Sets for Scene**. Every object gets its **own** material and texture set,
   named after the object, even when several objects shared one material or share the same
   mesh. All placeholders go into one shared `textures` folder and all objects are exported
   into one FBX (`<blend name>_scene/`). Running it again reuses what is there.

   **Every object is unwrapped first with
   [Smart Seams](https://github.com/mag-folio/smart-seams)** when that add-on is enabled, so
   Painter gets clean UVs. The **Unwrap** setting in the Whole Scene box chooses:
   - **All Objects** (default): objects that already have UVs are re-unwrapped too. Two
     exceptions: an object whose texture set has already been painted keeps its UVs (new
     UVs would scramble the painting; you get a message naming them), and a mesh Smart Seams
     already unwrapped isn't redone on the next press unless its geometry changed. So
     pressing the button again, or after Esc, carries on where it stopped.
   - **Only Without UVs**: only objects with no UV map, or one collapsed to a point or a
     line. Existing UVs are never changed.
   - **Off**: nothing is unwrapped.

   A mesh shared by several objects is unwrapped once. Smart Seams uses its own settings
   (sidebar > Smart Seams). If it fails on an object, that object keeps the UVs it had and
   you get a message. Without Smart Seams the objects are left as they are (Painter can
   auto-unwrap them on import) and you get a message naming them. Unwrapping a dense mesh
   can take a minute; the status bar shows which object is being unwrapped.
3. Press **Edit Scene in Painter**. Painter opens the whole scene, with one texture set per
   object, the Blender lighting and the object bounds already sent (see Painter Link below).
4. Use **Solo** in Painter's *Blender Link* panel to work on one object at a time, and
   export with the **Blender (Principled BSDF)** template as PNG. Every object in Blender
   updates from that one export.

**Big scenes are sized to your computer automatically.** The add-on reads the computer's
memory and graphics memory and uses measured Painter costs, so nothing can overload it:

- **One Painter project when it fits:** **Edit Scene in Painter** opens the whole scene as
  one project whenever the computer can hold it, lowering the texture size if needed
  (for example 965 objects on a 32 GB PC with a 12 GB graphics card open together at
  256 px). Painter's texture sets are set to that size for you, and the project is saved
  as `<blend name>_scene.spp` in the scene folder, so the next press reopens your work.
- **Parts only when it can't:** a scene too big even at 256 px is split into parts (objects
  from the same collection and group stay together), and the button opens the part that
  holds the selected object.
- **No freezing:** Create Sets for Scene runs in small steps with progress in the status
  bar; **Esc** stops it, and pressing it again finishes the rest.

The plan is made again on every press, so it follows the computer you are on. The add-on
preferences have *Texture Memory Budget* and *Max Texture Sets per Painter Project*
(0 = automatic). Placeholders are flat colours and are written small; untouched
placeholders from an earlier run are shrunk too, painted textures are never touched.

## Painter Link: Blender lighting, solo and orbit in Painter

Painter has no lights of its own and no way to show one object out of a big scene and
frame it. Painter Link adds both, through a small Painter plugin the add-on installs for you.

**In Blender:** select the mesh you want to paint and press **Send to Painter** (in the
*Create & Paint* sub-panel, *Painter Link* box). It sends three things:

- **Lighting.** Painter only lights with an environment map, so the add-on bakes one from
  the object's position with Cycles. It contains your world and everything around the
  object (bounce light, reflections and shadows from other objects; the object itself is
  hidden). Each lamp (sun, point, spot, area) is measured at the object and painted into the
  map as a small bright disc that gives the same amount of light from the same direction.
  The map is already turned to Painter's axes and includes your Blender exposure.
- **Where every object is.** Bounds for each material, which is how Painter names its
  texture sets.
- **The view.** Your 3D viewport, or the scene camera when you look through it.

The camera button next to it sends only the view, which is instant. **Edit in Substance
Painter** sends everything automatically before opening Painter (switch that off with
*Send when opening Painter*). Map size, samples, minimum lamp size and *Include
Surroundings* are in the add-on preferences.

**In Painter:** the plugin loads with Painter (restart Painter once after the first send).
It adds a **Blender Link** panel and a **Texture Plugger** menu:

- **Solo:** shows only one texture set and frames the camera on it. Painter's own Focus Mode
  is switched on for you. This works with a whole scene loaded. Double-click a set in the panel, or pick it and
  press *Solo*. While soloing, picking another set in Painter's Texture Set List frames
  that one. **Show All** leaves solo mode.
- **Orbit around it:** the ◀ ▲ ▼ ▶ buttons (and *Zoom +/-*) in the panel orbit around the
  selected object's centre at the current zoom. Alt + left-drag on the object also works;
  Painter then orbits around the point under the cursor.
- **Lighting:** the Blender lighting becomes the environment map automatically, and
  updates each time you press Send to Painter. Every object you sent keeps its own
  lighting; soloing it switches to that lighting.
- **Match Blender View:** puts Painter's camera where your Blender view is.

Both apps share the folder `Documents\Adobe\Adobe Substance 3D Painter\texture_plugger_link`
(the plugin goes into `...\python\startup`). Only maps still in use are kept there.

Notes:

- Blender's default *AgX* view transform and Painter's tone mapping differ, so contrast can
  look a little different even though the light is the same.
- Very small lamps are widened to *Min Lamp Size* (2 degrees) so they don't sparkle in
  Painter; reflections of them are softer than in Cycles.
- Texture sets are matched by material name. A set that isn't in the link (e.g. renamed)
  is framed as the whole scene.

## What gets built

| Map | Wiring | Colour space |
|---|---|---|
| Base Color / Albedo / Diffuse | Base Color (optionally × AO through a Mix Multiply) | sRGB |
| Ambient Occlusion | multiplied into Base Color | Non-Color |
| Metallic, Roughness, Specular (to *Specular IOR Level*) | direct | Non-Color |
| Gloss / Smoothness | Invert to Roughness | Non-Color |
| Normal OpenGL | Normal Map node to Normal | Non-Color |
| Normal DirectX | Separate Color, then G = 1 − G, then Combine Color, then Normal Map | Non-Color |
| Bump | Bump node chained after the Normal Map | Non-Color |
| Height / Displacement | Displacement node to Material Output (method *Displacement and Bump*) | Non-Color |
| Emission | Emission Color (strength is set to 1 if it was 0) | sRGB |
| Opacity / Alpha | Alpha (render method Dithered/Blended, transparent shadows on) | Non-Color |
| Transmission, Subsurface, Sheen, Coat, Coat Roughness, Anisotropy (+ rotation) | the matching *… Weight* inputs | Non-Color |
| Sheen Tint | Sheen Tint | sRGB |
| Packed ORM/ARM, RMA, MRA, HDRP MaskMap, Metallic-Smoothness | Separate Color (+ image alpha) into the right inputs | Non-Color, channel-packed alpha |

Colour maps stored as EXR stay linear instead of sRGB.

All image nodes share one *Texture Coordinate* node and one *Mapping* node. Everything sits
in a labelled frame with a fixed, repeatable layout. UDIM sequences (`name.1001.png`, or
`name_1001.png` when there are at least two tiles) load as one tiled image. Images that are
already loaded are reused instead of being loaded twice.

## Detection

Filenames are split on `_ - . space` and on camelCase. Keywords only match whole tokens
(or runs of whole tokens, so `Base_Color`, `BaseColor` and `basecolor` all match).

- **Which match wins:** the one closest to the end of the name, because map suffixes come
  after the set name. So `Metal_Plate_ao` is AO, not Metallic. On a tie, the longer,
  more specific keyword wins (`Sheen_Color` is Sheen Tint).
- **Single letters** (`_d`, `_n`, `_r`, `_m`, `_s`, `_h`) only count as the final suffix.
- **Tokens that are ignored:** resolution (`1k`…`16k`, `2048`), format (`png`, `jpg`…),
  `LOD0`, `VAR1` and bit depth (`16bit`, `NRM16`).
- **Choosing between duplicates:** first by preferred resolution, then by format priority
  (default: EXR first for normal/height, otherwise PNG > TIFF > TGA > EXR > JPG), then no
  variant, then higher bit depth.
- **Normal maps:** if neither OpenGL nor DirectX is named in the file, the preference
  setting decides (default OpenGL).

Naming conventions covered: Poly Haven, ambientCG, Quixel/Megascans, Substance,
Textures.com, FreePBR, Poliigon and Unreal/Unity suffixes. Every keyword list can be
edited in the add-on preferences, and there's a reset-to-defaults button.

## Development

```
python tests\test_detection.py          # pure-Python detection tests (no Blender)
python tests\test_scaffold.py           # pure-Python Create & Paint scaffolding tests (no Blender)
.\tests\run_blender_tests.ps1           # headless Blender test (isolated user dir)
.\tests\run_blender_tests.ps1 -Legacy   # same, loaded as a legacy add-on
.\tests\run_blender_tests.ps1 -Script adversarial_test.py   # drop path, reload, linked data...
.\tests\run_blender_tests.ps1 -Script scene_link_test.py   # Painter Link: lighting bake, bounds, view
.\tests\run_blender_tests.ps1 -Script scene_batch_test.py  # whole scene: unique materials, one FBX
.\tests\run_blender_tests.ps1 -Script scene_unwrap_test.py # whole scene: Smart Seams unwraps UV-less objects
python tests\test_painter_link.py       # link file contract, panorama maths, Painter camera maths
python tests\test_orbit.py              # Painter plugin orbit / zoom maths
python tests\test_scene_plan.py         # big scenes: texture size, parts, placeholder writer
.\tests\run_blender_tests.ps1 -Blender "C:\...\Blender 4.5\blender.exe"
.\build.ps1 -Test                       # build + validate manifest + all tests
```

The headless test sets `BLENDER_USER_RESOURCES` to a temporary folder, so your real
Blender preferences and extensions are never changed.

## Known limitations

- Applying the textures replaces any links you had going into the Principled BSDF inputs
  it uses (and into the Output's Displacement input). **Remove Textures** does not put
  those links back.
- The add-on wires into a Principled BSDF only. If the Material Output is driven by a
  different shader, a Principled BSDF is still created and textured, but it is not
  connected to the output (you'll get a warning).
- Tangent-space normal maps with Box projection are only approximate.
- Linked materials, library-override materials and objects whose mesh/curve data is linked
  are read-only: they are skipped with a message.
- Images that the add-on's removed nodes leave unused are deleted on re-apply/Remove
  (unless they have a fake user or unsaved paint).
- Create & Paint: Painter must export with the "Blender (Principled BSDF)" template as PNG.
  With any other template the file names don't match and Blender won't pick them up.
  The **Export to Blender** button in Painter's Blender Link panel (also in the Texture
  Plugger menu) always does exactly that, into the folder Blender watches. Painter's API
  can't add a preset to its own Export dialog, so the button is the way to make it stick.
- Create & Paint: if you change the geometry in Blender after the project was started, the
  mesh is not updated in Painter. Re-import the mesh in Painter (the add-on exports it again
  to `mesh/` each time you press **Edit in Substance Painter** without a saved `.spp`).
- The file browser and the drop menu need an interactive Blender. The headless tests
  call `wm.drop_import_file` and the import operator with `INVOKE_DEFAULT` the way Blender
  does, but they can't click the menu.
