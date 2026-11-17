# Painting a whole scene in Painter with Blender's lighting

A step-by-step guide to the workflow that gives the best match between what you paint in
Substance 3D Painter and what Blender renders: Smart Seams unwraps the scene, Texture
Plugger opens it in Painter, Cycles bakes the scene's lighting onto every object's UVs, and
you paint lit colours on top of that lighting.

It covers the first setup, the daily loop, coming back to a project later, changing the
lighting, and what to do when something looks wrong. Button names are the ones in Texture
Plugger 1.6.4 (Blender) and its Painter plugin 1.7.2.

## What you need

- Blender 4.5 or 5.0, with the **Smart Seams** and **Texture Plugger** add-ons installed
  and enabled (Edit > Preferences > Get Extensions > **v** menu > Install from Disk, then
  pick each add-on's zip from its `dist` folder).
- Adobe Substance 3D Painter (tested with 12.2).
- A graphics card is strongly recommended: the lighting bake uses it even if Cycles is
  set to *None* in Blender's preferences. On an RTX 5070 a 350-set office scene takes about
  2.5 minutes at a fixed 256 px and 128 samples, and roughly 30-60 minutes at the
  recommended Automatic size and 256 samples (several times slower on the CPU). You can
  keep working meanwhile, but Painter and the bake share the graphics card.

**After installing or updating an add-on, restart Blender.** Blender keeps running the
old code it loaded at start-up. An older version baked the lighting inside Blender itself
and froze it for many minutes, which looked exactly like a crash. The Painter plugin is
copied into Painter by Blender on every *Send to Painter*: restart Painter after an update
too.

## 1. Prepare the scene in Blender (once per .blend)

1. Save the .blend under the name you will keep. **The name decides the Painter project:**
   `Office.blend` paints in `Office_scene/Office_scene.spp` (inside the add-on's textures
   folder). Saving as another
   name later (`Office_v2.blend`) starts a *new*, empty Painter project with blank
   placeholder textures; your painting stays in the old one (see section 5).
2. Select every object you want to paint (box-select in the viewport or the Outliner).
3. Sidebar (`N`) > **Texture Plugger** > Whole Scene box > **Create Sets for Scene**.
   Every object gets its own material and texture set, and Smart Seams unwraps them all
   in the background (status bar shows progress, **Esc** stops and keeps what is done).
4. **Edit Scene in Painter**. Painter opens the whole scene as one project and saves it as
   `<blend name>_scene.spp`.

## 2. Bake the lighting (Blender)

The lighting Painter shows comes from one of two places:

| Source | How it looks | When it is used |
|---|---|---|
| **Cycles lightmap** (Bake Lighting to UVs) | Matches a Cycles render: within about 1% in the light, cast and contact shadows, bounce light | Sets that have one |
| Environment map (Send to Painter) | Light seen from one point: no cast shadows, 2x too bright in shadow, often bluer | Fallback for sets without a lightmap |

So always bake the lightmaps:

1. Select all meshes (`A` in the viewport).
2. Texture Plugger > Painter Link > **Bake Lighting to UVs**. It runs in a background
   Blender on a copy of the file: you can keep working, the status bar shows *baking
   lighting N / M sets, about X min left*, and **Esc** stops it (finished sets are kept).
3. With your main object active, press **Send to Painter**. This sends the environment
   lighting, the Material Preview lighting, object bounds and the view.

Settings (Edit > Preferences > Add-ons > Texture Plugger): keep *Lightmap Size* on
**Automatic** (each set gets its texture size, and up to 2048 px when its UVs are cut into
tiny pieces, so even small objects aren't blocky) and *UV Lighting Bake Samples* at **256**
(raise it if objects in deep shadow still look grainy). Lightmaps are denoised
automatically. If you changed these before, set them back here: saved preferences win
over the defaults.

### Without opening Blender's window

The same bake and send can run from a command prompt, with your own Blender preferences
and the installed add-on (the .blend is only read, never saved):

```
"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe" -b "C:\path\Office.blend" --python tools\bake_scene_lighting.py -- --send
```

Leave out `--send` to bake only. Run it once per .blend whose Painter project you want lit:
lightmaps are named after the texture sets, so those of several .blend files live side by
side and an older project keeps working.

## 3. Paint with the lighting (Painter)

The **Blender Link** panel (Window > Views) is split into colour-coded sections. Each button
has a badge saying which way it works: orange **B** → red **Pt** is *Blender → Painter*,
**Pt** → **B** is *Painter → Blender*, a lone **Pt** stays in Painter.

- **LIGHTING** (yellow bulb): only light moves, your textures are never touched.
- **TEXTURES** (blue swatch): your painted maps go to Blender or come back.
- **VIEW** (grey eye): camera only.

1. LIGHTING > *Light from Blender's*: **Render**.
2. **Bake Lighting into All Linked Sets…** (or *into Selected Set*). Each set gets a
   *Blender Lighting* layer (its Cycles lightmap, Multiply) and an empty **Lit Paint** layer
   above it. The list shows `[Cycles lightmap]` for sets lit this way. A set marked *no
   lighting of its own* has neither a lightmap nor its own environment: bake again in
   Blender (section 2) before trusting its colours.
3. Paint on **Lit Paint** (or layers above it), picking colours from the lit result. Press
   **C** to view the Base Color channel while painting (the material view would light it a
   second time). Layers *below* Blender Lighting are lit automatically, so you can still
   change the albedo there.
4. TEXTURES > **Send All Sets to Blender…**. It saves the project first, then writes the
   PNGs Blender uses. Blender shows sets with baked lighting **unlit** (the light is in the
   colour), so they look in Blender exactly as in Painter.

Anything that overwrites files, could hide your paint or deletes a layer asks first, with
**Cancel** as the default.

## 4. When the lighting changes

Moved a lamp or changed the world?

1. Blender: **Bake Lighting to UVs** again, then **Send to Painter**.
2. Painter: **Update Baked Lighting (N waiting)** lights up; press it. Lighting layers never
   change on their own unless you tick *Also update baked lighting layers automatically*.

## 5. Coming back later

- Open the project that belongs to the .blend you are working in: from that .blend press
  **Edit Scene in Painter**, or in Painter File > Open `<blend name>_scene.spp`.
- **Get … from Blender** brings the PNGs Blender shows back into a project as a *From
  Blender* layer. Use it only when Blender's textures are newer than the project (it warns
  you if they are older, or if the layer would cover your paint). Blank placeholder textures
  are skipped.
- Started a new .blend name by accident? Your painting is still in the old project and in
  the old `textures` folder; open that project.

## Troubleshooting

| What you see | Why | Fix |
|---|---|---|
| Colours in Painter bluer or brighter than Blender | Those sets have no Cycles lightmap and use the environment map | Bake Lighting to UVs in Blender, then Bake Lighting (or Update Baked Lighting) in Painter |
| Blender freezes for minutes on Bake Lighting to UVs | Blender is running an old add-on version from memory | Restart Blender after every add-on update |
| Painter says *Save failed* (log: "Failed to load image content from file ... lightmap_render_...exr", "null surface") | The project imported a lighting map that was later cleaned out of the shared link folder (plugin before 1.7.1) | Plugin 1.7.1+ copies lighting maps next to the .spp, so this no longer happens. If an older session is stuck: press **Send All Sets to Blender…** (it still exports when the save fails) to keep your newest paint, close Painter, reopen the project and use **Get All Sets from Blender…** |
| Small objects or finely cut sets look blocky or noisy | Lightmap too small for their UV pieces (a fixed 256 px before 1.6.4) | Lightmap Size *Automatic*, 256 samples, bake again, then **Update Baked Lighting** |
| Walls look fuzzy or grainy after Bake Lighting | Lightmaps from before 1.6.3 weren't denoised, and Painter multiplied the grain over the paint | Update, restart Blender, Bake Lighting to UVs again, then **Update Baked Lighting** in Painter |
| A floor or other object bakes black | Mirrored objects (negative scale) faced the wrong way in the bake (fixed in 1.6.2) | Update, restart Blender, bake again |
| `Cycles lightmap (outdated)` in the list | Lamps or world changed after the bake | Bake again in Blender, then Update Baked Lighting |
| Get from Blender loads nothing | Blender only has blank placeholders for those sets | Nothing to load yet: paint and Send to Blender first |
| A Blender process that can't be closed (Task Manager says Access denied) | Left over from an earlier freeze, stuck while shutting down | Restart the computer |

## Where things are

| What | Where |
|---|---|
| Painter projects and textures | the add-on's textures folder, in `<blend name>_scene/` (the `.spp` and a `textures` folder) |
| Lightmaps, lighting, link files | `Documents/Adobe/Adobe Substance 3D Painter/texture_plugger_link/` |
| Painter plugin | `Documents/Adobe/Adobe Substance 3D Painter/python/startup/texture_plugger_bridge.py` (installed by Blender) |
| Installed add-ons | `%APPDATA%/Blender Foundation/Blender/<version>/extensions/user_default/` |
