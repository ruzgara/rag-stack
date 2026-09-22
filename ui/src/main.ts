import "./app.css";
import { mount } from "svelte";
import App from "./App.svelte";

// Chrome matches :focus-visible on a <select> however it was focused, so
// app.css keys the select ring off the last input method instead.
const root = document.documentElement;
addEventListener("pointerdown", () => { root.dataset.modality = "pointer"; }, true);
addEventListener("keydown", (e) => {
  if (!e.metaKey && !e.ctrlKey && !e.altKey) root.dataset.modality = "keyboard";
}, true);

const app = mount(App, { target: document.getElementById("app")! });

export default app;
