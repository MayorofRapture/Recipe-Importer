"""Manually stage a whole cookbook PDF using a native Windows file dialog."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import threading

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from batch_store import BatchStore


def batch_choices(store: BatchStore) -> list[tuple[str, str]]:
    """Return display labels and durable IDs for existing batches."""
    return [
        (f"{batch['name']} ({batch['batch_id'][:8]})", batch["batch_id"])
        for batch in store.list_batches()
    ]


def path_has_link_component(path: Path | str) -> bool:
    """Reject files selected through a symlink or Windows junction."""
    current = Path(path).expanduser()
    while True:
        try:
            if current.is_symlink():
                return True
            is_junction = getattr(current, "is_junction", None)
            if callable(is_junction) and is_junction():
                return True
            if os.name == "nt":
                attributes = current.lstat().st_file_attributes
                if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    return True
        except FileNotFoundError:
            pass
        except OSError:
            return True
        if current.parent == current:
            return False
        current = current.parent


def stage_selected_pdf(store: BatchStore, batch_id: str, selected_path: Path | str) -> dict:
    """Stage a native-dialog selection through the existing bounded API."""
    if not str(selected_path or "").strip():
        return {"status": "cancelled", "accepted": False}
    source = Path(selected_path).expanduser()
    if path_has_link_component(source):
        return {
            "status": "rejected", "accepted": False,
            "filename": source.name,
            "reason": "The selected PDF path crosses a symlink or directory junction.",
        }
    return store.stage_parent_pdf(batch_id, source)


def choose_and_stage(store: BatchStore, batch_id: str, picker) -> dict:
    """Stage one selection, leaving batch state unchanged when the dialog is cancelled."""
    selected_path = picker()
    if not selected_path:
        return {"status": "cancelled", "accepted": False}
    return stage_selected_pdf(store, batch_id, selected_path)


def main() -> int:
    if os.name != "nt":
        print("The cookbook selector is intended for Windows.")
        return 1
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Run the cookbook selector on the Windows desktop thread.")

    import tkinter as tk
    from tkinter import filedialog, ttk

    from library_paths import resolve_library_path

    try:
        store = BatchStore(resolve_library_path())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Could not open the Recipe-Importer library: {exc}")
        return 1

    root = tk.Tk()
    root.title("Recipe-Importer · Stage cookbook")
    root.geometry("620x430")
    root.minsize(500, 340)

    ttk.Label(root, text="Select the batch for this cookbook", font=("Segoe UI", 12, "bold")).pack(
        anchor="w", padx=18, pady=(18, 5),
    )
    batch_box = ttk.Combobox(root, state="readonly", width=68)
    batch_box.pack(fill="x", padx=18)
    documents = ttk.Label(root, text="", justify="left", wraplength=570)
    documents.pack(anchor="w", fill="x", padx=18, pady=(16, 8))
    message = tk.StringVar(value="Choose an existing batch, then select a PDF.")
    message_label = ttk.Label(root, textvariable=message, justify="left", wraplength=570)
    message_label.pack(anchor="w", fill="x", padx=18, pady=(8, 4))

    choices: list[tuple[str, str]] = []

    def selected_batch_id() -> str:
        label = batch_box.get()
        return next((batch_id for choice, batch_id in choices if choice == label), "")

    def show_documents(_event=None) -> None:
        batch_id = selected_batch_id()
        batch = store.get(batch_id) if batch_id else None
        parents = (batch or {}).get("parent_documents", [])
        if not parents:
            documents.configure(text="No cookbook PDFs are staged in this batch yet.")
            return
        rows = ["Cookbooks already staged:"]
        rows.extend(
            f"• {item.get('original_filename', 'Cookbook PDF')} · {item.get('page_count', '?')} pages · "
            f"{item.get('size_bytes', 0):,} bytes"
            for item in parents
        )
        documents.configure(text="\n".join(rows))

    def refresh_batches() -> None:
        nonlocal choices
        prior_id = selected_batch_id()
        choices = batch_choices(store)
        batch_box.configure(values=[label for label, _batch_id in choices])
        selected = next((label for label, batch_id in choices if batch_id == prior_id), "")
        if not selected and choices:
            selected = choices[0][0]
        batch_box.set(selected)
        choose_button.configure(state="normal" if choices else "disabled")
        if choices:
            message.set("Select a PDF. It stays separate from ordinary recipe uploads.")
        else:
            message.set("Create a batch in Recipe-Importer first, then refresh this list.")
        show_documents()

    def choose_pdf() -> None:
        batch_id = selected_batch_id()
        if not batch_id:
            message.set("Choose an existing batch first.")
            return
        result = choose_and_stage(
            store,
            batch_id,
            lambda: filedialog.askopenfilename(
                parent=root,
                title="Select a cookbook PDF",
                filetypes=[("PDF documents", "*.pdf")],
            ),
        )
        status = result.get("status")
        if status == "cancelled":
            message.set("Selection cancelled. No files or batch records changed.")
        elif status == "staged":
            message.set(
                f"Staged {result.get('original_filename')} · {result.get('page_count')} pages · "
                f"{result.get('size_bytes', 0):,} bytes."
            )
        elif status == "duplicate":
            prior = store.get(result.get("existing_batch_id", "")) or {}
            message.set(f"This exact PDF is already staged in “{prior.get('name', 'another batch')}”.")
        elif status == "relinked":
            message.set("Restored the exact previously staged PDF at its existing identity.")
        else:
            message.set(f"Could not stage {result.get('filename', 'the selected PDF')}: "
                        f"{result.get('reason', 'The PDF was rejected.')}")
        show_documents()

    batch_box.bind("<<ComboboxSelected>>", show_documents)
    controls = ttk.Frame(root)
    controls.pack(anchor="w", padx=18, pady=14)
    choose_button = ttk.Button(controls, text="Choose cookbook PDF…", command=choose_pdf)
    choose_button.pack(side="left")
    ttk.Button(controls, text="Refresh batches", command=refresh_batches).pack(side="left", padx=(8, 0))
    ttk.Label(
        root,
        text="PDFs are validated and copied into the local library. Limit: 256 MiB and 2,000 pages.",
        wraplength=570,
    ).pack(anchor="w", padx=18, pady=(8, 12))

    refresh_batches()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
