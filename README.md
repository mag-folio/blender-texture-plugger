# Texture Plugger

A Blender add-on (Extension format, Blender 4.5 and 5.0+) that hooks PBR texture sets up to a
Principled BSDF material. It detects each map from its filename and wires everything up
(colour spaces, normal/bump/displacement, packed maps). Run it again and you get the same
result, never duplicated nodes. Pure Python with no dependencies. It makes no network calls.

![A stone floor and two painted metal crates, textured in one go by Texture Plugger](docs/images/render_after.jpg)

**Painting a whole scene in Substance 3D Painter with Blender's lighting?** Follow the
step-by-step guide: [docs/WORKFLOW.md](docs/WORKFLOW.md).

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

1. Build the zip with `.\build.ps1` (or `python build.py`). This creates `dist\texture_plugger-<version>.zip`.
2. In Blender go to **Edit > Preferences > Get Extensions**, open the **v** menu (top right),
   choose **Install from Disk...** and pick the zip.
3. Check that **Texture Plugger** is enabled.
4. After installing a newer version, **restart Blender**: it keeps running the code it
   loaded at start-up until then.

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

   A mesh shared by several objects is unwrapped once, and meshes with exactly the same
   geometry (copies that were made single user) are solved once and get the same UVs.
   **Max Faces** (0 = no limit, the default) can skip very dense meshes; they keep their UVs
   and are named in a message.

   A scene with many or dense meshes is unwrapped **in the background, several meshes at
   once**: Texture Plugger starts a few extra Blender processes (half of the processor's
   threads), each runs Smart Seams on one mesh at a time, dense meshes first, and the UVs and
   seams are copied back as each mesh finishes. Blender keeps drawing and stays responsive
   meanwhile, the status bar shows what is being unwrapped, and Esc stops at once and keeps
   what is done. The result is the same as pressing Smart Seams Unwrap on each object. Small
   jobs, and Smart Seams with *Front* set to the scene camera, are unwrapped one by one in
   Blender itself, as before (Esc then stops between objects). Smart Seams uses its own settings
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
It adds a **Blender Link** panel and a **Texture Plugger** menu. Both are split into three
coloured sections, so light and textures can't be mixed up:

- **LIGHTING** (yellow, bulb): only light moves; your painted textures are never touched.
- **TEXTURES** (blue, colour swatch): your painted maps go to Blender or come back.
- **VIEW** (grey, eye): camera only; nothing in your file changes. It folds away.

Every button carries a badge saying which way it works: orange **B** → red **Pt** is
*Blender → Painter*, **Pt** → **B** is *Painter → Blender*, a lone **Pt** stays in Painter.
The menu says the same in words. The *Selected* line under the list names the texture set
the *Selected* buttons act on and warns when it has no lighting of its own.

Safety: anything that overwrites Blender's files, could hide your paint or deletes a layer
asks first, with **Cancel** as the default (Enter and Esc never do damage). *Get from
Blender* warns when the new layer would sit above your own layers, or when Blender's files
are older than your last Painter save (Painter has the newer work). Only one action runs at
a time and the buttons grey out while it does, so a double click does nothing twice. The
coloured status line at the bottom says what the last action did.

- **Solo:** shows only one texture set and frames the camera on it. Painter's own Focus Mode
  is switched on for you. This works with a whole scene loaded. Double-click a set in the panel, or pick it and
  press *Solo*. While soloing, picking another set in Painter's Texture Set List frames
  that one. **Show All** leaves solo mode.
- **Orbit around it:** the ← ↑ ↓ → buttons (and *Zoom +/-*) in the panel orbit around the
  selected object's centre at the current zoom. Alt + left-drag on the object also works;
  Painter then orbits around the point under the cursor.
- **Lighting:** the Blender lighting becomes the environment map automatically, and
  updates each time you press Send to Painter. Every object you sent keeps its own
  lighting; soloing it switches to that lighting.
- **Match Blender Camera:** puts Painter's camera where your Blender view is.
- **Bake Lighting:** paints the Blender lighting into the Base Color, so you can paint and
  colour-pick lit colours (see below).

### Bake Lighting: paint with the lighting in the Base Color

Pick a texture set in the *Blender Link* panel and press **Bake Lighting into Selected Set**
(or *Bake Lighting into All Linked Sets…*). If the set has no lighting of its own (it would
be lit as if it stood where the last object sent is, which can look too blue or too bright),
or already has a lighting layer of the other kind, the plugin asks first. It then:

1. bakes the *World Space Normal* and *AO* mesh maps first if the set doesn't have them yet
   (the lighting needs them to know which way each pixel faces);
2. adds a **Blender Lighting** layer at the top of the set: Painter's own *Baked Lighting
   Environment* filter, fed that object's Blender lighting map, on *Passthrough* so it
   lights the Base Color of everything below it;
3. adds an empty **Lit Paint** layer above it and selects it.

Paint on *Lit Paint* (or above it), picking colours from the lit result in the Base Color
channel view. Layers below
*Blender Lighting* are lit automatically, so you can still change the albedo under it. View
the **Base Color channel** (press **C**) while painting: Painter's material view lights the
already-lit colours a second time. When Blender sends new lighting (or you switch
between *Render* and *Material Preview*), the layers keep what they show and **Update Baked
Lighting (N waiting)** lights up: press it to give them the new lighting. It asks first and
warns when a layer would change kind (Cycles lightmap vs filter), because that layer is made
again and loses settings you changed on it. Tick *Also update baked lighting layers
automatically* to have them follow every send (a change of kind still waits for the button).
*Remove Baked Lighting from Selected…* deletes the layer (after asking) and keeps your *Lit
Paint* layer.

**Back in Blender:** exports (the panel's *Send … to Blender* or Painter's own export
dialog) write `texture_plugger_lit.json` next to the textures. Blender then shows those
sets **unlit**: Base Color → Emission (strength 1) → Material Output, with Alpha through a
Transparent mix. Blender's lamps don't light them a second time. The Principled BSDF network
stays in the material, unplugged, and comes back if you export the set again without the
lighting layer. Scenes that show unlit sets switch to the **Standard** view transform (no
look, exposure 0, gamma 1), so the colours look exactly as painted. The previous settings
come back when no unlit set is left. Both are add-on preferences (*Show Painter-Lit
Textures Unlit*, *Standard View for Unlit*); the panel shows how many materials are unlit.

Limits of the filter: its lighting is the map seen from the object's centre, so it can't
show shadows that other objects cast across the surface. Measured, those areas come out
1.9-2.4x too bright; open areas are within about 5-10%. The filter's *Horizontal Rotation*
can be tweaked in the layer, and the plugin keeps your value. For the real shadows, bake
the lighting to UVs first:

### Bake Lighting to UVs (Cycles): the exact render lighting

In Blender, select the meshes and press **Bake Lighting to UVs** (*Painter Link* box). For
each texture set, Cycles bakes the light the object receives onto its own UVs: the Diffuse
pass, Direct + Indirect, without colour. That includes every lamp, the world, bounce light,
and cast and contact shadows. It runs in a **background Blender** on a copy of the scene, so
you can keep working. The status bar shows progress and the time left, and **Esc** stops it.
Sets finished so far are kept, even if you stop it or close Blender. It bakes on your
graphics card when the computer has one, even if Cycles is set to *None* in Blender's
preferences (the CPU is several times slower; your preferences aren't changed).

Settings (add-on preferences): *Lightmap Size* and *UV Lighting Bake Samples* (256).
*Automatic* size (the default) gives each set its own texture size, so one light pixel
covers one paint pixel, and doubles it (up to 2048 px) for sets cut into small UV pieces
until those pieces are at least 12 pixels across; pieces too small to see in Painter are
ignored. At a fixed 256 px, small objects and finely cut sets came out blocky and grainy.
Fixed sizes (128 to 1024 px, or the texture set size) are still there, and the bake time
grows with the pixel count. Every lightmap is then denoised with OpenImageDenoise (Blender's own denoiser,
in a throwaway scene, well under a second per map): raw Cycles grain was multiplied over
the paint and made walls look fuzzy. It includes your scene exposure. All objects that use the same
texture set are baked together into one map; material slots no face uses are skipped
(Painter makes no texture set for them). Mirrored objects (negative scale) bake the right
way up. To bake every mesh of a .blend from a command prompt, see
`tools/bake_scene_lighting.py` and the [workflow guide](docs/WORKFLOW.md). The
operator's *Studio HDRI Too* option (off by default, doubles the time) adds a second bake lit
by the Material Preview studio HDRI. That bake is Cycles with that HDRI, shadows and bounce
included, so it isn't the same as EEVEE's Material Preview. Without it, Painter's *Material
Preview* lighting mode keeps using the filter.

Your scene is put back exactly afterwards: render engine and samples, bake settings, world,
lamps, selection, mode and the materials. For the bake, the material is a plain diffuse
with its own Base Color, so light bouncing off the object keeps its real colour. A set that
was already exported with baked lighting has light in its Base Color, so a flat grey stands
in for its albedo; bounce light off that object is then approximate. The maps go to the
shared `texture_plugger_link` folder and are listed in `blender_lightmaps.json`. If you move
a lamp or change the world and press *Send to Painter* after baking, the Painter panel marks
the set *Cycles lightmap (outdated)*: bake again.

Then press **Bake Lighting** in Painter as before. A set that has a Cycles lightmap gets a
**Blender Lighting** *fill* layer on Base Color, with *Multiply* blending, instead of the
filter: albedo x Cycles light, which is the diffuse colour a Cycles render shows. In the test
scene (a plane, one sun, a box casting a shadow) the lightmap is within 1% of a Cycles
render in the light and within 15% in the shadow, which there gets bounce light only; the
filter was 1.9-2.4x too bright in such shadows. The panel lists such sets as *Cycles
lightmap*. That set's Base Color channel switches to 16-bit float, because light brighter
than 1 would otherwise be clipped before the Multiply. That doubles the channel's memory;
exports still write normal PNGs. After baking again in Blender, press *Update Baked
Lighting*. Sets without a lightmap keep using the filter.

Glossy reflections are view-dependent, so they aren't in either version. The UV bake uses
the object's active UV map, which must be the one Painter paints on.

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
  The **Send … to Blender** buttons in Painter's Blender Link panel (also in the Texture
  Plugger menu) always do exactly that, into the folder Blender watches. Painter's API
  can't add a preset to its own Export dialog, so the button is the way to make it stick.
- Create & Paint: if you change the geometry in Blender after the project was started, the
  mesh is not updated in Painter. Re-import the mesh in Painter (the add-on exports it again
  to `mesh/` each time you press **Edit in Substance Painter** without a saved `.spp`).
- The file browser and the drop menu need an interactive Blender. The headless tests
  call `wm.drop_import_file` and the import operator with `INVOKE_DEFAULT` the way Blender
  does, but they can't click the menu.
