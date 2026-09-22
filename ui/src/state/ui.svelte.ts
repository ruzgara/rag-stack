// Cross-cutting UI state: toasts, the popup stack, the promise-based
// confirm/ask dialogs, and which turn the sources panel is showing.

export interface Toast {
  id: number;
  message: string;
  kind: "ok" | "err";
}

/** Every popup the app has. Opening one pushes it; Escape / the backdrop /
 *  × pops the top; closeAll clears the stack (the design's closeAll). */
export type Modal =
  | { kind: "workspaces" }
  | { kind: "libraries" }
  | { kind: "library"; id: string }
  | { kind: "sharing"; id: string }
  | { kind: "source"; turnKey: string; handle: string }
  | { kind: "upload"; libraryId?: string; folder?: boolean }
  | { kind: "groups" }
  | { kind: "settings" }
  | { kind: "trash"; tab?: "chats" | "workspaces" | "archived" };

export type FieldType = "text" | "password" | "email" | "textarea" | "select" | "checkbox";
export interface Field {
  name: string;
  label: string;
  type?: FieldType;
  value?: string | boolean;
  options?: { value: string; label: string }[];
  required?: boolean;
  placeholder?: string;
  hint?: string;
}
export type FormResult = Record<string, string | boolean>;

interface DialogBase {
  title: string;
  kicker?: string;
  body?: string;
  okLabel: string;
  danger?: boolean;
}
export type Dialog =
  | (DialogBase & { kind: "confirm"; resolve: (ok: boolean) => void })
  | (DialogBase & { kind: "ask"; fields: Field[]; resolve: (r: FormResult | null) => void });

export const RAIL_WIDTH = 268;
export const SOURCES_WIDTH = 336;
const WIDTHS_KEY = "rag.columnWidths";

function readWidth(key: "rail" | "sources", fallback: number): number {
  try {
    const w = JSON.parse(localStorage.getItem(WIDTHS_KEY) ?? "{}")[key];
    return typeof w === "number" && w > 0 ? w : fallback;
  } catch {
    return fallback;
  }
}

class UiState {
  toasts = $state<Toast[]>([]);
  modals = $state<Modal[]>([]);
  dialog = $state<Dialog | null>(null);
  /** Which turn's sources the right panel shows; null = the last turn. */
  selectedTurnKey = $state<string | null>(null);
  /** Source handles to flash in the panel (hovering/clicking a citation). */
  focusedSources = $state<string[]>([]);
  /** Narrow screens: the rail and the sources panel become drawers. */
  railOpen = $state(false);
  sourcesOpen = $state(false);
  userMenuOpen = $state(false);
  /** Wide screens: user-dragged column widths, remembered per browser. */
  railWidth = $state(readWidth("rail", RAIL_WIDTH));
  sourcesWidth = $state(readWidth("sources", SOURCES_WIDTH));

  private toastSeq = 0;

  saveWidths(): void {
    try {
      localStorage.setItem(WIDTHS_KEY, JSON.stringify({ rail: this.railWidth, sources: this.sourcesWidth }));
    } catch {
      /* storage unavailable: widths just don't persist */
    }
  }

  toast(message: string, kind: Toast["kind"] = "ok"): void {
    const id = ++this.toastSeq;
    this.toasts.push({ id, message, kind });
    setTimeout(() => this.dismissToast(id), kind === "err" ? 9000 : 4500);
  }

  dismissToast(id: number): void {
    this.toasts = this.toasts.filter((t) => t.id !== id);
  }

  open(m: Modal): void {
    this.userMenuOpen = false;
    this.railOpen = false;
    this.modals.push(m);
  }

  /** Replaces the top popup (e.g. prev/next inside the source viewer). */
  replace(m: Modal): void {
    if (this.modals.length) this.modals[this.modals.length - 1] = m;
    else this.modals.push(m);
  }

  close(): void {
    this.modals.pop();
  }

  closeAll(): void {
    this.modals = [];
    this.userMenuOpen = false;
  }

  confirm(opts: { title: string; body?: string; okLabel?: string; danger?: boolean; kicker?: string }): Promise<boolean> {
    return new Promise((resolve) => {
      this.dialog?.kind === "confirm" ? this.dialog.resolve(false) : this.dialog?.resolve(null);
      this.dialog = { kind: "confirm", okLabel: "OK", ...opts, resolve };
    });
  }

  ask(opts: { title: string; fields: Field[]; okLabel?: string; body?: string; kicker?: string }): Promise<FormResult | null> {
    return new Promise((resolve) => {
      this.dialog?.kind === "confirm" ? this.dialog.resolve(false) : this.dialog?.resolve(null);
      this.dialog = { kind: "ask", okLabel: "Save", ...opts, resolve };
    });
  }

  settleDialog(result: boolean | FormResult | null): void {
    const d = this.dialog;
    this.dialog = null;
    if (!d) return;
    if (d.kind === "confirm") d.resolve(result === true || (typeof result === "object" && result !== null));
    else d.resolve(typeof result === "object" ? result : null);
  }

  focusSources(handles: string[], turnKey?: string): void {
    if (turnKey) this.selectedTurnKey = turnKey;
    this.focusedSources = handles;
  }
}

export const ui = new UiState();

/** Wraps a handler so a rejection becomes a toast rather than an unhandled
 *  console error. */
export function guard<A extends unknown[]>(fn: (...args: A) => Promise<unknown>): (...args: A) => void {
  return (...args: A) => {
    fn(...args).catch((e: unknown) => {
      ui.toast(e instanceof Error ? e.message : String(e), "err");
    });
  };
}
