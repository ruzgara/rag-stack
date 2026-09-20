// Cross-cutting UI state: toasts, the citation<->source cross-highlight, and
// the promise-based <dialog> idiom ported from admin.html:199-229. Simple
// yes/no confirmations use window.confirm() directly in components, matching
// admin.html's own confirmed() (admin.html:196) -- only prompt()-style text
// entry and alert()-style result reporting are what test-ui.html got wrong
// (plan defect 5), not confirm() itself.

export interface Toast {
  id: number;
  message: string;
  kind: "ok" | "err";
}

export type DialogFieldType = "text" | "password" | "email" | "select" | "checkbox";
export interface DialogField {
  name: string;
  label: string;
  type?: DialogFieldType;
  value?: string | boolean;
  options?: { value: string; label: string }[];
  required?: boolean;
  placeholder?: string;
}

export type DialogResult = Record<string, string | boolean>;

interface DialogRequest {
  title: string;
  fields: DialogField[];
  okLabel: string;
  resolve: (result: DialogResult | null) => void;
}

class UiState {
  toasts = $state<Toast[]>([]);
  focusedSources = $state<number[]>([]);
  /** Which turn's sources the SourcesPanel is showing. Set by a turn's
   *  "Sources (N)" footer button (defect 8: this works even on an errored
   *  turn, unlike test-ui.html's onclick bound only at `done`) and by
   *  clicking a [n] citation. */
  selectedTurnKey = $state<string | null>(null);
  dialogRequest = $state<DialogRequest | null>(null);

  private toastSeq = 0;

  toast(message: string, kind: Toast["kind"] = "ok"): void {
    const id = ++this.toastSeq;
    this.toasts.push({ id, message, kind });
    setTimeout(() => this.dismissToast(id), kind === "err" ? 9000 : 4000);
  }

  dismissToast(id: number): void {
    this.toasts = this.toasts.filter((t) => t.id !== id);
  }

  focusSources(nums: number[], turnKey?: string): void {
    this.focusedSources = nums;
    if (turnKey) this.selectedTurnKey = turnKey;
  }

  clearFocus(): void {
    this.focusedSources = [];
  }

  showTurnSources(turnKey: string): void {
    this.selectedTurnKey = turnKey;
    this.focusedSources = [];
  }

  /** A small modal form, mirroring admin.html:199-229's ask(title, fields,
   *  okLabel). Resolves null on cancel. */
  ask(title: string, fields: DialogField[], okLabel = "Save"): Promise<DialogResult | null> {
    return new Promise((resolve) => {
      this.dialogRequest = { title, fields, okLabel, resolve };
    });
  }

  resolveDialog(result: DialogResult | null): void {
    this.dialogRequest?.resolve(result);
    this.dialogRequest = null;
  }
}

export const ui = new UiState();

/** guard(fn): wraps a handler so a rejection becomes a toast instead of an
 *  unhandled console error -- admin.html's convention (admin.html:192-195). */
export function guard<A extends unknown[]>(fn: (...args: A) => Promise<void>): (...args: A) => void {
  return (...args: A) => {
    fn(...args).catch((e: unknown) => {
      ui.toast(e instanceof Error ? e.message : String(e), "err");
    });
  };
}
