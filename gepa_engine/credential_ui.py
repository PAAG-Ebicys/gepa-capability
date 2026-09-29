"""A local masked entry; only the save callback receives the credential."""

from collections.abc import Callable

from .config import ConfigError, Connection
from .providers import ProviderError


def capture_credential(connection: Connection, save: Callable[[str], None]) -> bool:
    """Return whether saved. Do not import Tk or create a window until requested."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        raise ConfigError("La captura gráfica necesita Tkinter. Repara la instalación de Python incluyendo Tcl/Tk y vuelve a ejecutar set-key --ui.") from None
    try:
        root = tk.Tk()
    except tk.TclError:
        raise ConfigError("No se pudo abrir la ventana de credenciales. Ejecuta set-key --ui desde la sesión de escritorio del usuario; no desde una sesión remota sin escritorio.") from None
    saved = False
    try:
        root.title("GEPA · Credencial")
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"API key de {connection.provider} · {connection.id}").pack(anchor="w")
        ttk.Label(frame, text="Pega tu clave con Ctrl+V. Se guardará cifrada para este usuario.").pack(anchor="w", pady=(6, 12))
        entry = ttk.Entry(frame, show="*", width=60)
        entry.pack(fill="x")
        status = ttk.Label(frame, text="", wraplength=440)
        status.pack(anchor="w", pady=(8, 0))

        def close():
            entry.delete(0, tk.END)
            root.destroy()

        def submit(event=None):
            nonlocal saved
            key = entry.get().strip()
            if not key:
                status.configure(text="Pega una API key antes de guardar.")
                return
            try:
                save(key)
            except (ProviderError, ConfigError):
                status.configure(text="No se pudo guardar la clave. Revisa su formato y los permisos de la carpeta de configuración.")
                return
            finally:
                key = ""
            saved = True
            close()

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancelar", command=close).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Guardar", command=submit).pack(side="left")
        root.protocol("WM_DELETE_WINDOW", close)
        root.bind("<Return>", submit)
        root.bind("<Escape>", lambda event: close())
        root.after(100, entry.focus_force)
        root.mainloop()
        return saved
    except tk.TclError:
        raise ConfigError("La ventana de credenciales dejó de estar disponible. Vuelve a ejecutar set-key --ui en la sesión de escritorio.") from None
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except tk.TclError:
            pass
