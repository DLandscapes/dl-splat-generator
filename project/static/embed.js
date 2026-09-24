/* Read-only shell for exported web scenes.
 *
 * Reuses the same Viewer and Tools as the app, so an embedded scene renders and
 * measures identically -- it simply never enables an editing tool. */
import { Viewer } from "./viewer.js";
import { Tools } from "./tools.js";

const $ = (id) => document.getElementById(id);

function status(text, ms = 3000) {
  const el = $("status");
  if (!text) { el.hidden = true; return; }
  el.textContent = text;
  el.hidden = false;
  if (ms) setTimeout(() => { el.hidden = true; }, ms);
}

async function main() {
  const viewer = new Viewer($("canvas"), $("overlay"));
  const tools = new Tools(viewer, $("overlay"));
  viewer.onPick = null;   // read-only: clicks never add geometry

  let scene;
  try {
    const res = await fetch("scene.json");
    if (!res.ok) throw new Error(`scene.json not found (HTTP ${res.status})`);
    scene = await res.json();
  } catch (err) {
    status(`⚠ ${err.message}`, 0);
    return;
  }

  const title = scene.title || scene.source?.name || "Scene";
  document.querySelector("#title span").textContent = title;

  status("Loading scene…", 0);
  try {
    await viewer.loadUrl(scene.source.name, scene.source.name);
  } catch (err) {
    status(`⚠ ${err.message}`, 0);
    return;
  }

  tools.fromJSON(scene);
  if (typeof scene.view?.flip === "boolean") viewer.setFlip(scene.view.flip);
  const d = scene.view?.display;
  if (d) {
    if (d.lodScale) viewer.setLodScale(d.lodScale);
    if (d.blur != null) viewer.setBlur(d.blur);
  }
  if (scene.section?.enabled) viewer.setSection(scene.section, { outline: false });
  if (scene.view?.current) {
    viewer.home = scene.view.current;
    viewer.setCameraState(scene.view.current);
  }

  // handle for the browser-side tests, mirroring app.js
  window.dl = { viewer, tools, scene };

  const bar = $("views");
  if (tools.viewpoints.length) {
    for (const v of tools.viewpoints) {
      const b = document.createElement("button");
      b.textContent = v.name;
      b.onclick = () => tools.goToViewpoint(v.id);
      bar.appendChild(b);
    }
  }
  const home = document.createElement("button");
  home.textContent = "Reset view";
  home.onclick = () => viewer.resetView();
  bar.appendChild(home);

  status(null);
}

main();
