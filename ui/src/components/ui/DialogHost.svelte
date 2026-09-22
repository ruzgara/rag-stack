<script lang="ts">
  // The confirm / ask dialogs (ui.confirm, ui.ask). Sits above every popup.
  import { ui, type Field, type FormResult } from "../../state/ui.svelte";

  let values = $state<FormResult>({});
  let form: HTMLFormElement | undefined = $state();

  $effect(() => {
    const d = ui.dialog;
    if (d?.kind === "ask") {
      values = Object.fromEntries(d.fields.map((f) => [f.name, f.value ?? (f.type === "checkbox" ? false : "")]));
    }
    if (d) queueMicrotask(() => form?.querySelector<HTMLElement>("input, textarea, select, button.ok")?.focus());
  });

  function submit(e: SubmitEvent): void {
    e.preventDefault();
    const d = ui.dialog;
    if (!d) return;
    if (d.kind === "ask") {
      const trimmed = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, typeof v === "string" ? v.trim() : v]));
      ui.settleDialog(trimmed);
    } else {
      ui.settleDialog(true);
    }
  }

  function cancel(): void {
    ui.settleDialog(ui.dialog?.kind === "confirm" ? false : null);
  }

  function onKey(e: KeyboardEvent): void {
    if (e.key === "Escape" && ui.dialog) {
      e.preventDefault();
      e.stopPropagation();
      cancel();
    }
  }

  const id = (f: Field) => `dlg-${f.name}`;
</script>

<svelte:window onkeydowncapture={onKey} />

{#if ui.dialog}
  {@const d = ui.dialog}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="backdrop" role="presentation" onclick={(e) => { if (e.target === e.currentTarget) cancel(); }}>
    <form class="dialog" bind:this={form} onsubmit={submit} aria-labelledby="dlg-title">
      {#if d.kicker}<div class="kicker">{d.kicker}</div>{/if}
      <h4 id="dlg-title">{d.title}</h4>
      {#if d.body}<p class="body">{d.body}</p>{/if}
      {#if d.kind === "ask"}
        {#each d.fields as f (f.name)}
          {#if f.type === "checkbox"}
            <label class="check">
              <input type="checkbox" checked={!!values[f.name]} onchange={(e) => (values[f.name] = e.currentTarget.checked)} />
              {f.label}
            </label>
          {:else}
            <div class="field">
              <label for={id(f)}>{f.label}</label>
              {#if f.type === "textarea"}
                <textarea id={id(f)} class="input" rows="3" placeholder={f.placeholder} required={f.required}
                          value={String(values[f.name] ?? "")} oninput={(e) => (values[f.name] = e.currentTarget.value)}></textarea>
              {:else if f.type === "select"}
                <select id={id(f)} class="input" value={String(values[f.name] ?? "")} onchange={(e) => (values[f.name] = e.currentTarget.value)}>
                  {#each f.options ?? [] as o (o.value)}<option value={o.value}>{o.label}</option>{/each}
                </select>
              {:else}
                <input id={id(f)} class="input" type={f.type ?? "text"} placeholder={f.placeholder} required={f.required}
                       value={String(values[f.name] ?? "")} oninput={(e) => (values[f.name] = e.currentTarget.value)} />
              {/if}
              {#if f.hint}<div class="hint">{f.hint}</div>{/if}
            </div>
          {/if}
        {/each}
      {/if}
      <div class="actions">
        <button type="button" class="btn btn-ghost" onclick={cancel}>Cancel</button>
        <button type="submit" class="btn ok {d.danger ? 'btn-danger btn-secondary' : 'btn-primary'}">{d.okLabel}</button>
      </div>
    </form>
  </div>
{/if}

<style>
  .backdrop {
    position: fixed; inset: 0; z-index: 90; display: grid; place-items: center; padding: 20px;
    background: color-mix(in srgb, var(--color-neutral-900) 35%, transparent);
  }
  .dialog {
    width: min(440px, 100%); display: flex; flex-direction: column; gap: 12px;
    padding: 22px 24px 18px; border-radius: var(--radius-lg); background: var(--color-bg); box-shadow: var(--shadow-lg);
  }
  h4 { margin: 0; font-size: 20px; }
  .body { margin: 0; font-size: 14px; line-height: 1.55; color: var(--color-neutral-800); }
  .check { display: flex; gap: 8px; align-items: center; font-size: 14px; cursor: pointer; }
  .hint { font-size: 11.5px; color: var(--color-neutral-600); margin-top: 4px; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 6px; }
</style>
