<script lang="ts">
  // Mirrors admin.html:199-229's ask(): a <form method="dialog"> whose two
  // submit buttons carry `value="ok"`/`value="cancel"`, so the browser
  // itself sets dialog.returnValue and fires exactly one `close` event --
  // no manual double-close bookkeeping.
  import { ui, type DialogField } from "../state/ui.svelte";

  let dialogEl: HTMLDialogElement | undefined = $state();
  let values = $state<Record<string, string | boolean>>({});

  $effect(() => {
    const req = ui.dialogRequest;
    if (!req || !dialogEl) return;
    values = Object.fromEntries(req.fields.map((f) => [f.name, f.value ?? (f.type === "checkbox" ? false : "")]));
    dialogEl.showModal();
    queueMicrotask(() => dialogEl?.querySelector<HTMLElement>("input, select, textarea")?.focus());
  });

  function fieldValue(f: DialogField): string | boolean {
    return values[f.name] ?? (f.type === "checkbox" ? false : "");
  }

  function setValue(name: string, v: string | boolean): void {
    values = { ...values, [name]: v };
  }

  function onClose(): void {
    const req = ui.dialogRequest;
    if (!req || !dialogEl) return;
    ui.resolveDialog(dialogEl.returnValue === "ok" ? values : null);
  }
</script>

<dialog bind:this={dialogEl} onclose={onClose}>
  {#if ui.dialogRequest}
    <form method="dialog">
      <b>{ui.dialogRequest.title}</b>
      {#each ui.dialogRequest.fields as f (f.name)}
        {#if f.type === "select"}
          <label for="f_{f.name}">{f.label}</label>
          <select id="f_{f.name}" value={fieldValue(f)} onchange={(e) => setValue(f.name, e.currentTarget.value)}>
            {#each f.options ?? [] as o (o.value)}
              <option value={o.value}>{o.label}</option>
            {/each}
          </select>
        {:else if f.type === "checkbox"}
          <label style="margin-top: 10px; display: flex; gap: 6px; align-items: center;">
            <input
              type="checkbox"
              style="width: auto;"
              checked={!!fieldValue(f)}
              onchange={(e) => setValue(f.name, e.currentTarget.checked)}
            />
            {f.label}
          </label>
        {:else}
          <label for="f_{f.name}">{f.label}</label>
          <input
            id="f_{f.name}"
            type={f.type ?? "text"}
            value={fieldValue(f)}
            required={f.required}
            placeholder={f.placeholder}
            oninput={(e) => setValue(f.name, e.currentTarget.value)}
          />
        {/if}
      {/each}
      <div class="row" style="margin-top: 16px; justify-content: flex-end;">
        <button value="cancel">Cancel</button>
        <button class="primary" value="ok">{ui.dialogRequest.okLabel}</button>
      </div>
    </form>
  {/if}
</dialog>
