# How to create Nova Skin wallpapers with Blender

Ten years ago the only way to build a wallpaper template was by hand — placing every part in Cinema 4D or Mine-imator and lining up the UVs yourself (the old [Cinema 4D](https://forum.novaskin.me/t/how-to-create-wallpaper-templates-with-cinema-4d/50) and [Mine-imator](https://forum.novaskin.me/t/how-to-create-wallpapers-for-nova-skin-with-mine-imator/10304) guides). This is the modern way: build a scene in **Blender**, press one button, and get an **interactive wallpaper** that anyone can re-skin **right in the browser** — no re-rendering.

You render the scene **once**. The exported wallpaper lets viewers:

- drop in **any Minecraft skin** and see it relit instantly,
- switch **classic (Steve) / slim (Alex)** arms,
- toggle scenery layers on and off.

> 🎬 *[video: 10-second demo — swapping skins live on a finished wallpaper]*

---

## What you'll need

- **Blender 4.2 or newer**, running normally (with the interface — the exporter can't run in `--background` mode).
- The **NovaSkin Export** add-on (this guide — install below).
- The **Thomas Rig Legacy** rig — the Minecraft character rig the exporter is built around.
- A little Blender comfort: moving the camera, adding lights, placing blocks. You don't need to be an expert.

---

## Part 1 — Install the NovaSkin Export add-on

The add-on is pending review on the Blender Extensions platform. Until it's approved, install it from GitHub:

1. Go to the **[releases page](https://github.com/novaskin/novaskin-blender-addon/releases)** and download the latest **`novaskin_export-1.3.0.zip`**.
2. Open Blender and **drag the `.zip` straight onto the Blender window**.
   *(Or: `Edit ▸ Preferences ▸ Get Extensions ▸ ⌄ ▸ Install from Disk…` and pick the zip.)*
3. Make sure **“NovaSkin Export”** is enabled in the add-on list.

> 📷 *[screenshot: dragging the zip onto Blender + the enabled add-on]*

To update later, just install the newer zip the same way.

---

## Part 2 — Install the Thomas Rig Legacy rig

The wallpaper characters come from the **Thomas Rig Legacy** rig. Install it once from the Blender Extensions platform:

1. Open **`Edit ▸ Preferences ▸ Get Extensions`**, search for **“Thomas Rig Legacy”**, and install it — or grab it from its [extension page](https://extensions.blender.org/add-ons/thomas-rig-legacy/).
2. Enable it.

The NovaSkin panel will tell you (and link you) if it can't find the rig, so you'll know if this step is missing.

---

## Part 3 — Build your scene

Start a new file and set the stage. Nothing here is NovaSkin-specific — it's just a normal Blender scene:

1. **Save your `.blend`** somewhere with room to spare. The export writes a `novaskin/` folder **right next to your saved file**, so an unsaved file can't export.
2. **Add a camera** and frame your shot. Whatever the active camera sees is the wallpaper — set your resolution in `Output Properties` (e.g. 1920×1080 or 4K).
3. **Add lighting** — a Sun for direction plus some fill works well. The light and shadows you set here get baked into the wallpaper.
4. **Build the environment** — blocks, terrain, water, foliage, mobs… whatever your scene needs. This becomes the background and the foreground around your characters.

> 📷 *[screenshot: a simple example scene — camera, sun, a few blocks]*

**Tip:** the characters are re-skinnable in the browser, but the **environment is baked as-is** (it's a static image). Put the effort where it counts.

---

## Part 4 — Add your player(s)

This is where the Minecraft characters go in.

### Add the rig

1. In the 3D viewport, open the **Add menu** (`Shift + A`) → **Thomas Rig Legacy**.
2. A character rig appears. Move / rotate / pose it to taste. You can add **several** — every Thomas rig in the scene becomes a separate re-skinnable player in the wallpaper.

> 📷 *[screenshot: Add menu ▸ Thomas Rig Legacy]*

> ℹ️ **You don't need to apply a skin in Blender.** The wallpaper tool re-skins each character in the browser, so whatever skin the rig has is ignored on export — a plain default rig is fine. (Both **classic and slim** arms are exported, so viewers can switch either way.)

### ⚠️ Turn on “No Face” (disable the facial expressions)

The Thomas rig has an animatable **3D face** (eyes, eyebrows, mouth) meant for animation. Those 3D features **don't re-texture correctly**: when a viewer drops their own skin onto the character in the browser, the skin's flat face can't wrap onto the rig's sculpted 3D face. Switching to the flat Minecraft skin head fixes it.

- In the rig's **“Thomas Rig Legacy” sidebar tab → Design Settings**, enable **“No Face”**.
- Do this on **every** player rig in the scene.

> 📷 *[screenshot: the “No Face” toggle enabled in Design Settings]*

This swaps the expressive head for the clean Minecraft skin head. It's the single most important setting for a good wallpaper.

---

## Part 5 — (Optional) Mark toggleable scenery layers

Want a mob, a tree, or a prop that viewers can **turn on and off** in the browser? Mark it as an optional layer:

1. Select the object (or a whole rig / collection), open the **NovaSkin** sidebar tab.
2. Under **Layers**, hit **“Mark as Optional Layer.”**
   - A selected mesh marks just itself; a selected rig marks the whole rig; with nothing selected it marks the **active collection**.
3. Marked layers show up in the panel with an **✕** to remove them.

Each optional layer is exported separately (correctly occluded, with its own shadow) so the web tool can toggle it. Player rigs can't be marked — they're always players.

> 📷 *[screenshot: an object marked as an optional layer + the layer list]*

---

## Part 6 — Export

1. Open the **NovaSkin** tab in the 3D viewport sidebar (`N`).
2. Check the **Rig** section — it lists the players it detected. If it says no rig was found, revisit Part 2/4.
3. (Optional) In **Output**, choose where the `novaskin/` folder goes — the default is right next to your `.blend`. Set render **samples** under **Quality** if you want a cleaner bake.
4. Hit **Render**.

> 📷 *[screenshot: the NovaSkin panel — detected players + the Render button]*

A progress bar runs while it works (background, character meshes, light, shadows, scenery). You can **cancel any time** with the Cancel button or `Esc` — the scene is always restored exactly as it was. When it finishes, use **“Open Output Folder”** to find your `novaskin/` folder.

**That's your wallpaper** — a small folder of files plus a `manifest.json` describing how they fit together.

---

## Part 7 — Test it in the browser

1. Click **“Open Wallpaper Tool”** in the panel, or go to **[minecraft.novaskin.me/wallpapers/tools/blender](https://minecraft.novaskin.me/wallpapers/tools/blender/)**.
2. **Load your export folder** — select the `novaskin/` folder you just created (the one containing `manifest.json`).
3. Your scene appears. Now try it:
   - **Drop in different skins** — per player — and watch them relight to match your scene.
   - **Switch classic / slim** arms.
   - **Toggle** your optional layers and the character's 2nd layer (hat / jacket / sleeves).

> 🎬 *[video: loading the folder in the tool and swapping a skin]*

If it looks right here, it'll look right as a wallpaper. 🎉

---

## Troubleshooting & tips

- **“No rig found.”** The character must be a **Thomas Rig Legacy** rig, and the rig add-on must be installed and enabled. The panel links you to the rig if it's missing.
- **The face looks wrong / 3D-ish after re-skinning.** You forgot **“No Face”** on that player (Part 4).
- **Export won't start.** Save your `.blend` first — the output folder is written next to it. You also need an **active camera** and to run Blender **with its interface** (not `--background`).
- **The environment doesn't change when I re-skin.** That's expected — only the **characters** are re-skinnable; the scenery is baked. Use **optional layers** for anything you want toggleable.
- **Cleaner result:** raise the render **samples** in the panel's Quality section before the final export.

---

*Built something? Post it in the thread — we'd love to see it.*
