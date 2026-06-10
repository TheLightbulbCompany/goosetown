# Slim Godot Web Export Template — Feasibility Memo (Phase 0 spike)

**Date:** 2026-06-09 · **Task:** V4 (mobile-full-experience) · **Status:** research only, nothing built
**Gates:** this memo gates a future plan `2026-06-XX-slim-godot-template.md`

## Problem

The web viewer ships the **official** Godot 4.6.2 web template: every engine subsystem (3D renderer, 3D physics, XR, CSG, GridMap, glTF, audio codecs…) is compiled in, but GooseTown is pure 2D. Current payload (three-number story): `goosetown.wasm` **36 MB raw** / **8.9 MB wire as served today** (Vercel dynamic brotli, measured via `curl` full GET with `Accept-Encoding: br` against `dev.goosetown.isol8.co`) / **6.2 MB br-11 ceiling** (max-quality brotli, context only — not what Vercel serves); pck is **8.2 MB raw / 6.8 MB wire**, giving a total first-visit critical path of **~15.7 MB as served**. A custom size-optimized template would cut the wasm without touching game code.

## 1. Toolchain

Per the official docs ([Compiling for the Web, 4.6](https://docs.godotengine.org/en/4.6/engine_details/development/compiling/compiling_for_web.html)):

| Requirement | Godot 4.6 minimum | Local status (this machine, 2026-06-09) |
|---|---|---|
| Emscripten | **4.0.0+** (4.4 needed 3.1.62+; 4.5+ bumped to 4.0.0) | `emcc` **not installed** |
| SCons | 4.0+ | `scons` **not installed** |
| Python | 3.8+ | `/opt/homebrew/bin/python3` present |

Install: `brew install scons` + emsdk via git clone → `emsdk install latest && emsdk activate latest && source emsdk_env.sh`. **Pin the emsdk version** — there is a known class of breakage where newer Emscripten versions break custom web templates ([godot#105369](https://github.com/godotengine/godot/issues/105369)); match the version Godot's own 4.6 buildsystem CI uses (check `godotengine/build-containers` for the 4.6 branch) rather than blindly using `latest`.

Source: clone `godotengine/godot` at tag **`4.6.2-stable`** — the template version string must exactly match the exporting editor (4.6.2) or the export dialog rejects it.

## 2. Build command + build profile

What the project actually uses (scanned `apps/godot/scripts/`, `maps/`, `scenes/` in this worktree):

- **Nodes (scenes):** Sprite2D, StaticBody2D, CollisionShape2D, Marker2D, TileMapLayer, NavigationRegion2D/NavigationPolygon, **NavigationAgent2D**, Camera2D, CharacterBody2D, Node2D, plus Control-suite UI (Label, RichTextLabel, Button, containers). Tree/CodeEdit/FileDialog hits come from the **gdUnit4 addon only** (test framework, not viewer scenes).
- **Classes (scripts):** WebSocketPeer (16×), JavaScriptBridge (14×), NavigationAgent2D/NavigationServer2D (server-side movement — but the nav nodes live in `Isol88Map.tscn`, which **the web viewer loads too**, so 2D navigation must stay enabled in the template), StreamPeerTCP/TCPServer (server protocol, harmless to keep), Tween, RegEx.
- **Not used:** anything 3D, XR, CSG, GridMap, glTF/FBX, multiplayer (no MultiplayerAPI/ENet/WebRTC — raw WebSocketPeer only), **audio** (zero AudioStreamPlayer nodes, zero ogg/mp3/wav assets — music is the React-side `MusicButton`), video, custom shaders (no `.gdshader` files).

Build (from godot source root; `threads=no` matches the preset's `variant/thread_support=false`):

```bash
scons platform=web target=template_release production=yes optimize=size threads=no \
      build_profile=/path/to/goosetown.build
# output: bin/godot.web.template_release.wasm32.nothreads.zip
```

`production=yes` implies `lto=full` + `debug_symbols=no` (verify against 4.6.2 SConstruct; LTO showed no wasm size win in the amann.dev measurements) ([Optimizing a build for size](https://docs.godotengine.org/en/4.6/engine_details/development/compiling/optimizing_for_size.html)). `optimize=size` is the web-target default but is kept explicit here for documentation. 4.5+ also offers `optimize=size_extra` for a further squeeze. Optional extra pass: `wasm-opt -Oz` on the extracted wasm (the amann.dev write-up got 19 → 13 MB from it on a fully-minimal 4.3 build).

Proposed `goosetown.build` (build-profile JSON format per [godot#62996](https://github.com/godotengine/godot/pull/62996); options can equivalently go on the scons CLI, which sidesteps the editor-profile-generator naming bugs in [godot#104965](https://github.com/godotengine/godot/issues/104965)):

```json
{
  "type": "build_profile",
  "disabled_classes": [],
  "disabled_build_options": {
    "disable_3d": true,
    "disable_physics_3d": true,
    "disable_navigation_3d": true,
    "disable_xr": true,
    "module_openxr_enabled": false,
    "module_webxr_enabled": false,
    "module_mobile_vr_enabled": false,
    "module_csg_enabled": false,
    "module_gridmap_enabled": false,
    "module_gltf_enabled": false,
    "module_fbx_enabled": false,
    "module_enet_enabled": false,
    "module_webrtc_enabled": false,
    "module_upnp_enabled": false,
    "module_ogg_enabled": false,
    "module_vorbis_enabled": false,
    "module_minimp3_enabled": false,
    "module_theora_enabled": false,
    "module_raycast_enabled": false,
    "module_lightmapper_rd_enabled": false,
    "module_meshoptimizer_enabled": false,
    "module_vhacd_enabled": false,
    "module_basis_universal_enabled": false,
    "module_ktx_enabled": false
  }
}
```

The JSON above is informational/documentation only; prefer passing the equivalent flags directly on the scons CLI, which avoids the known editor profile-generator naming bugs ([godot#104965](https://github.com/godotengine/godot/issues/104965)).

**Keep enabled:** websocket, 2D navigation (`disable_navigation_2d` must stay false — viewer loads `Isol88Map.tscn` with NavigationRegion2D nodes), 2D physics (StaticBody2D/CharacterBody2D), regex, text_server_adv (RichTextLabel in viewer UI; swapping to the fallback text server is a phase-2 experiment, not day 1), advanced GUI (only gdUnit4 needs Tree/CodeEdit — could be reclaimed later by export-filtering the addon out of the pck, then `disable_advanced_gui=yes`). Verify exact option names against `4.6.2-stable`'s SConstruct before building — module flag names occasionally shift between minors.

## 3. Integration

- **Export preset:** set `custom_template/release="/path/to/web_nothreads_release.zip"` in `apps/godot/export_presets.cfg` (key already exists, currently `""`). **Note: `export_presets.cfg` is gitignored** (`apps/godot/.gitignore:11`) — the template path lives only on the exporting machine, so the template zip location must be documented/conventional (e.g. `~/.godot-templates/goosetown/4.6.2/`).
- **Export flow unchanged:** exports run locally via `/Applications/Godot.app/.../Godot --headless --export-release "Web" ...` and the resulting `goosetown.{wasm,pck,js}` are **committed** to `apps/frontend/public/godot/` — so the slim wasm flows to Vercel through the existing commit-the-artifacts path with zero CI changes required for v1.
- **Optional CI job (later):** GitHub Actions on `ubuntu-latest`: checkout godot @ `4.6.2-stable`, `mymindstorm/setup-emsdk` (pinned + cached), `pip install scons`, run the scons command (~30–60 min cold; scons cache + emsdk cache cut reruns substantially), upload the template zip as a release artifact. This is for reproducibility/bus-factor, not a launch requirement.

## 4. Expected size cut

Best grounded data point: [amann.dev "Optimize Size of Godot Releases" (2025, Godot 4.3)](https://amann.dev/blog/2025/godot_web_size/), corroborated by [Popcar's minify write-up](https://popcar.bearblog.dev/how-to-minify-godots-build-size/):

| 4.3 web wasm config | Raw size |
|---|---|
| Official template (baseline) | 33 MB |
| `optimize=size` only | 32 MB |
| + disable_3d + fallback text server | 25 MB |
| + all modules disabled (kitchen-sink minimal) | 19 MB |
| + `wasm-opt` pass on the minimal build | 13 MB (→ **2.4 MB brotli**) |

GooseTown's profile sits between "disable_3d" and "everything off" (we keep websocket, nav-2D, 2D physics, advanced text server, full GUI). Scaling to our 36 MB 4.6 baseline:

- **Projected raw wasm: ~22–27 MB** (≈ 25–39% cut against raw 36 MB). Note: the ~20 MB floor cited in some references requires switching to `text_server_fb` (fallback text server), which breaks RichTextLabel rendering (speech bubbles in the viewer) — that is a phase-2 experiment only and is excluded from this profile.
- **Projected wire wasm (served via Vercel dynamic brotli): ~5.4–6.7 MB** vs 8.9 MB today (proportional estimate against the 8.9 MB wire baseline — **flagged as estimate, medium/low confidence**; brotli ratios on wasm are fairly stable across configs per the amann data, but only a real build + served-wire measurement settles it). To re-measure the served wire for any build, use a full `curl` GET with `Accept-Encoding: br` against the deployed URL — do not rely on local brotli-11 numbers, which are higher quality than what Vercel serves dynamically.
- **Total first-visit wire:** ~15.7 MB → ~12–14 MB (the 6.8 MB pck wire is untouched), i.e. **~13–20% total page weight reduction**.

Confidence: **medium** on the wasm-relative numbers (one detailed source + one corroborating source, different Godot minor); **low** until a local build is measured and served wire re-checked.

## 5. Risks

1. **Engine-version lockstep.** Template must be rebuilt from the exact tag for every Godot upgrade (4.6.2 → 4.6.3 → 4.7…). Each upgrade now costs a 30–60 min template rebuild + size re-verification. Mitigated by the CI job; still a permanent tax.
2. **Build-profile drift.** Disabling a module the game later adopts (e.g. Godot-side audio, glTF props, MultiplayerAPI) fails at **runtime in the web viewer only** — desktop editor and headless server (which run official/full builds) keep working, so the breakage is easy to miss until a browser test. Mitigation: comment the profile inline, add a smoke-test step (existing Playwright `smoke.spec.ts` canvas check catches total init failure; a class-load failure mid-scene needs the console-error check).
3. **Emscripten fragility.** Custom-template breakage with mismatched emsdk versions is a real, recurring issue ([godot#105369](https://github.com/godotengine/godot/issues/105369)). Mitigation: pin emsdk to godot 4.6 CI's version.
4. **Server/viewer scene sharing.** The headless game server and web viewer load the same `.tscn` files; any class disabled in the template but present in a shared scene = viewer load error. This is why nav-2D and 2D physics stay in. Future scene changes must respect the profile.
5. **Maintenance burden is asymmetric.** One person (local template zip, gitignored preset path) is a bus-factor-1 setup until the CI job exists.

## 6. Recommendation: **GO (conditional)**

- **Against the gate** ("projected wire-size cut < 30% → NO-GO"): measured on the **wasm wire payload** (as served by Vercel dynamic brotli, baseline 8.9 MB) — the projected cut is **~25–39%**, which straddles the 30% bar. The projection could land either side depending on which subsystems actually contribute to the wasm in 4.6.2. The "build locally, measure real served wire, then decide" step in the execution plan below is what resolves this ambiguity — the memo cannot call a firm GO or NO-GO without that data. Measured on **total first-visit wire** the cut is only ~13–20%, which may fail the gate; if the plan's gate intends total page weight, this is a **NO-GO as a standalone measure** and only worth doing bundled with pck reduction (texture/import audit, gdUnit4 export-filter). State which denominator the plan means before committing.
- **Recommended execution shape (GO path):**
  1. **Local one-time build first** (½–1 day incl. toolchain setup): brew scons + pinned emsdk, clone `4.6.2-stable`, build with the profile above, wire into local `export_presets.cfg`, re-export, **measure real raw size and served wire size** (via `curl` full GET with `Accept-Encoding: br` against the deployed dev URL — not local brotli-11 which is higher quality than Vercel serves) and run the dev-site smoke test on the slim build.
  2. **Gate on the measurement:** if real served-wire wasm < 6.2 MB (≥30% cut vs 8.9 MB baseline), commit the re-exported artifacts; otherwise stop with data in hand.
  3. **Then** add the GitHub Actions template-build job for reproducibility, and document the template path convention.
- Do **not** start with the CI job or with aggressive extras (fallback text server, `disable_advanced_gui`, `optimize=size_extra`, `wasm-opt`) — those are phase-2 squeezes once the conservative profile is proven in-browser.

**Sources:** [Compiling for the Web (4.6 docs)](https://docs.godotengine.org/en/4.6/engine_details/development/compiling/compiling_for_web.html) · [Optimizing a build for size (4.6 docs)](https://docs.godotengine.org/en/4.6/engine_details/development/compiling/optimizing_for_size.html) · [Feature build profiles PR godot#62996](https://github.com/godotengine/godot/pull/62996) · [Editor profile naming bug godot#104965](https://github.com/godotengine/godot/issues/104965) · [Custom web templates vs emsdk godot#105369](https://github.com/godotengine/godot/issues/105369) · [amann.dev web-size write-up](https://amann.dev/blog/2025/godot_web_size/) · [Popcar minify write-up](https://popcar.bearblog.dev/how-to-minify-godots-build-size/)
