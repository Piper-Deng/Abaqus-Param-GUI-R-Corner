# -*- coding: utf-8 -*-
"""
Abaqus parameter GUI launcher for Mesh_Generation_R_Corner_f_anyangle.py

Features:
1) Auto-read editable parameters from the script's "Global Parameters (EDIT THESE)" block.
2) Let user edit parameters in GUI.
3) Mode 1: run script and open model in Abaqus/CAE.
4) Mode 2: run script in noGUI and export INP to user-selected folder.

This tool does not modify the original script file in-place.
It generates a temporary script with overridden parameters for each run.
"""

from __future__ import print_function

import os
import re
import sys
import shutil
import tempfile
import threading
import subprocess

try:
    import tkinter as tk
    import tkinter.ttk as ttk
    import tkinter.filedialog as fd
    import tkinter.messagebox as mb
    import tkinter.scrolledtext as st
except ImportError:
    # Keep Python 2 fallback via dynamic import to avoid static-analysis false alarms in Python 3.
    tk = __import__("Tkinter")
    ttk = __import__("ttk")
    fd = __import__("tkFileDialog")
    mb = __import__("tkMessageBox")
    st = __import__("ScrolledText")

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except Exception:
    Image = None
    ImageTk = None
    HAS_PIL = False


PARAM_ASSIGN_RE = re.compile(r"^(\s*)([A-Za-z_]\w*)(\s*=\s*)(.*?)(\s*(#.*)?)\r?\n?$")


def _extract_year_token(text):
    m = re.search(r"(20\d{2})", text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    return 0


def _label_from_command(path_or_name):
    base = os.path.basename(path_or_name)
    base_no_ext = os.path.splitext(base)[0]
    lower = base_no_ext.lower()
    year = _extract_year_token(path_or_name)

    if "abaqus" in lower:
        prefix = "Abaqus"
    elif "abq" in lower:
        prefix = "ABQ"
    else:
        prefix = "Abaqus"

    if year:
        return "%s %s (%s)" % (prefix, year, path_or_name)
    return "%s (%s)" % (prefix, path_or_name)


def _walk_with_depth(root_dir, max_depth=4):
    root_dir = os.path.abspath(root_dir)
    if not os.path.isdir(root_dir):
        return
    for cur_root, dirs, files in os.walk(root_dir):
        rel = os.path.relpath(cur_root, root_dir)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirs[:] = []
            continue
        yield cur_root, dirs, files


def detect_abaqus_commands():
    """Auto-detect available Abaqus launch commands on Windows."""
    results = []
    seen = set()

    candidate_names = [
        "abaqus",
        "abaqus2025",
        "abaqus2024",
        "abaqus2023",
        "abq2025",
        "abq2024",
        "abq2023",
    ]

    def add_cmd(cmd_text):
        if not cmd_text:
            return
        norm = os.path.normcase(os.path.abspath(cmd_text)) if os.path.sep in cmd_text or ":" in cmd_text else cmd_text.lower()
        if norm in seen:
            return
        seen.add(norm)
        year = _extract_year_token(cmd_text)
        results.append({
            "label": _label_from_command(cmd_text),
            "command": cmd_text,
            "year": year,
        })

    # 1) PATH lookup for common command names.
    for name in candidate_names:
        full = shutil.which(name)
        if full:
            add_cmd(full)
        else:
            # Keep bare command if it is a shell alias in Abaqus command prompt.
            if name == "abaqus":
                add_cmd(name)

    # 2) Search common installation roots for command bat/cmd/exe.
    roots = []
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        v = os.environ.get(key, "").strip()
        if v:
            roots.append(v)
    roots.extend([
        "C:/SIMULIA",
        "C:/DassaultSystemes",
        "C:/ProgramData/SIMULIA",
    ])

    allowed_ext = (".bat", ".cmd", ".exe")
    for root in roots:
        if not os.path.isdir(root):
            continue

        for cur_root, dirs, files in _walk_with_depth(root, max_depth=5):
            cur_lower = cur_root.lower()
            # Prune directories unlikely to contain Abaqus launch scripts.
            dirs[:] = [d for d in dirs if d.lower() not in ("windows", "winsxs", "microsoft", "python", "nodejs")]

            if "simulia" not in cur_lower and "abaqus" not in cur_lower and "commands" not in cur_lower:
                continue

            for fn in files:
                fn_lower = fn.lower()
                if not (fn_lower.startswith("abaqus") or fn_lower.startswith("abq")):
                    continue
                if not fn_lower.endswith(allowed_ext):
                    continue
                add_cmd(os.path.join(cur_root, fn))

    # Sort by year desc, then label.
    results.sort(key=lambda x: (-int(x.get("year") or 0), x.get("label", "")))
    return results


def find_parameter_block(lines):
    """Locate [Global Parameters] block and return (start_idx, end_idx)."""
    start = None
    end = None

    for i, line in enumerate(lines):
        if "Global Parameters" in line and "EDIT THESE" in line:
            start = i + 1
            break

    if start is None:
        return None, None

    for i in range(start, len(lines)):
        line = lines[i]
        if re.match(r"^\s*#\s*Derived", line):
            end = i
            break

    if end is None:
        end = len(lines)

    return start, end


def parse_parameters(script_text):
    """Extract simple assignment parameters from Global Parameters block."""
    lines = script_text.splitlines(True)
    start, end = find_parameter_block(lines)
    if start is None:
        return {}, None, None

    params = {}
    for i in range(start, end):
        line = lines[i]
        if line.strip().startswith("#"):
            continue
        m = PARAM_ASSIGN_RE.match(line)
        if not m:
            continue

        name = m.group(2)
        value = m.group(4).strip()
        comment = (m.group(6) or "").strip()
        params[name] = {
            "value": value,
            "comment": comment,
            "line": i,
        }

    return params, start, end


def apply_parameter_overrides(script_text, overrides):
    """Apply parameter value overrides in Global Parameters block only."""
    lines = script_text.splitlines(True)
    start, end = find_parameter_block(lines)
    if start is None:
        raise RuntimeError("Could not find 'Global Parameters (EDIT THESE)' block in script.")

    for i in range(start, end):
        line = lines[i]
        m = PARAM_ASSIGN_RE.match(line)
        if not m:
            continue

        indent, name, eq_part, _, tail = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        if name not in overrides:
            continue

        new_value = overrides[name].strip()
        if not new_value:
            raise ValueError("Parameter '%s' cannot be empty." % name)

        line_ending = "\n"
        if line.endswith("\r\n"):
            line_ending = "\r\n"
        elif line.endswith("\n"):
            line_ending = "\n"

        new_line = "%s%s%s%s%s%s" % (indent, name, eq_part, new_value, tail or "", line_ending)
        lines[i] = new_line

    return "".join(lines)


def build_inp_export_tail(out_dir):
    """Append code that writes .inp and moves it to output directory."""
    out_dir_literal = repr(out_dir)
    return """
\n# ===== Auto-generated by Abaqus_Param_GUI_R_Corner.py =====
import os
import shutil


def _safe_job_name(name):
    chars = []
    for ch in str(name):
        if ch.isalnum() or ch in ('_', '-'):
            chars.append(ch)
        else:
            chars.append('_')
    text = ''.join(chars)
    return text[:80] or 'Job_Auto'


try:
    _model_name = MODEL_NAME
except:
    _keys = mdb.models.keys()
    _model_name = _keys[-1]

_out_dir = %s
if not os.path.isdir(_out_dir):
    os.makedirs(_out_dir)

_job_name = _safe_job_name('Job_' + str(_model_name))
if _job_name in mdb.jobs.keys():
    try:
        del mdb.jobs[_job_name]
    except:
        pass

_job = mdb.Job(name=_job_name, model=_model_name, type=ANALYSIS)
_job.writeInput(consistencyChecking=OFF)

_src_inp = os.path.join(os.getcwd(), _job_name + '.inp')
_dst_inp = os.path.join(_out_dir, _job_name + '.inp')

if os.path.isfile(_dst_inp):
    os.remove(_dst_inp)

if os.path.isfile(_src_inp):
    shutil.move(_src_inp, _dst_inp)
    print('INP exported to: %%s' %% _dst_inp)
else:
    print('Warning: INP not found at %%s' %% _src_inp)
""" % out_dir_literal


class AbaqusParamGui(object):
    def __init__(self, root):
        self.root = root
        self.root.title("Abaqus Parameter GUI - R Corner")
        self.root.geometry("1080x760")

        script_default = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "Mesh_Generation_R_Corner_f_anyangle.py")

        self.script_path_var = tk.StringVar(value=script_default)
        self.abaqus_cmd_var = tk.StringVar(value="abaqus")
        self.abaqus_version_var = tk.StringVar(value="Auto")
        self.inp_out_var = tk.StringVar(value=os.path.join(os.path.dirname(script_default), "inp_output"))
        self.figure_path = os.path.join(os.path.dirname(script_default), "Figure.jpeg")
        self.abaqus_options = []
        self.abaqus_label_to_cmd = {}
        self.figure_photo = None
        self.figure_pil_image = None

        self.param_entries = {}
        self.current_params = {}
        self.current_script_text = ""

        self._build_ui()
        self.refresh_abaqus_versions(initial=True)
        self.reload_parameters()
        self.load_reference_figure()

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="Script Path:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.script_path_var, width=95).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(top, text="Browse", command=self._choose_script).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="Reload Params", command=self.reload_parameters).grid(row=0, column=3, padx=4)

        ttk.Label(top, text="Abaqus Version:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.version_combo = ttk.Combobox(top, textvariable=self.abaqus_version_var, state="readonly", width=65)
        self.version_combo.grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        self.version_combo.bind("<<ComboboxSelected>>", self._on_version_selected)
        ttk.Button(top, text="Refresh Versions", command=self.refresh_abaqus_versions).grid(row=1, column=2, padx=4, pady=(8, 0))

        ttk.Label(top, text="Abaqus Command:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.abaqus_cmd_var, width=95).grid(row=2, column=1, sticky="ew", padx=6, pady=(8, 0))

        ttk.Label(top, text="INP Output Dir (Mode 2):").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.inp_out_var, width=95).grid(row=3, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(top, text="Browse", command=self._choose_inp_dir).grid(row=3, column=2, padx=4, pady=(8, 0))

        top.columnconfigure(1, weight=1)

        middle = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        middle.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Left: parameter editor, Right: structural figure preview.
        left_panel = ttk.Frame(middle)
        right_panel = ttk.Frame(middle)
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right_panel.pack(side=tk.LEFT, fill=tk.BOTH, padx=(10, 0))

        self.canvas = tk.Canvas(left_panel, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(left_panel, orient="vertical", command=self.canvas.yview)
        self.param_frame = ttk.Frame(self.canvas)

        self.param_frame.bind("<Configure>", self._on_param_frame_configure)
        self.canvas.create_window((0, 0), window=self.param_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        fig_title_bar = ttk.Frame(right_panel)
        fig_title_bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(fig_title_bar, text="Structure Reference Figure", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)

        self.figure_label = ttk.Label(right_panel, anchor="center")
        self.figure_label.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        button_bar = ttk.Frame(self.root, padding=10)
        button_bar.pack(side=tk.TOP, fill=tk.X)

        self.btn_mode1 = ttk.Button(
            button_bar,
            text="Mode 1: Run and Open in Abaqus/CAE",
            command=self.run_mode_open_cae,
        )
        self.btn_mode1.pack(side=tk.LEFT)

        self.btn_mode2 = ttk.Button(
            button_bar,
            text="Mode 2: Run and Export INP",
            command=self.run_mode_export_inp,
        )
        self.btn_mode2.pack(side=tk.LEFT, padx=10)

        self.log_text = st.ScrolledText(self.root, height=14)
        self.log_text.pack(side=tk.TOP, fill=tk.BOTH, expand=False, padx=10, pady=(0, 10))

    def _on_param_frame_configure(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _choose_script(self):
        path = fd.askopenfilename(
            title="Choose Abaqus Script",
            filetypes=[("Python Script", "*.py"), ("All files", "*.*")],
        )
        if path:
            self.script_path_var.set(path)

    def _choose_inp_dir(self):
        path = fd.askdirectory(title="Choose INP Output Folder")
        if path:
            self.inp_out_var.set(path)

    def load_reference_figure(self):
        fig_path = self.figure_path
        if not fig_path or not os.path.isfile(fig_path):
            self.figure_label.config(image="", text="No figure found.")
            self.figure_photo = None
            self.figure_pil_image = None
            self.log("Reference figure not found.")
            return

        if not HAS_PIL:
            self.figure_label.config(image="", text="Pillow is not installed, image preview disabled.")
            self.figure_photo = None
            self.figure_pil_image = None
            self.log("Pillow is not installed. Install with: pip install Pillow")
            return

        try:
            self.figure_pil_image = Image.open(fig_path)
            display_img = self.figure_pil_image.copy()
            # Keep the embedded figure compact for a cleaner right panel.
            display_img.thumbnail((420, 320), Image.LANCZOS)
            self.figure_photo = ImageTk.PhotoImage(display_img)
            self.figure_label.config(image=self.figure_photo, text="")
            self.log("Reference figure loaded.")
        except Exception as exc:
            self.figure_label.config(image="", text="Failed to load figure.")
            self.figure_photo = None
            self.figure_pil_image = None
            self.log("Failed to load figure: %s" % exc.__class__.__name__)

    def refresh_abaqus_versions(self, initial=False):
        options = detect_abaqus_commands()
        self.abaqus_options = options
        self.abaqus_label_to_cmd = {}

        labels = []
        for item in options:
            label = item["label"]
            labels.append(label)
            self.abaqus_label_to_cmd[label] = item["command"]

        if not labels:
            labels = ["Manual (use command field below)"]
            self.version_combo["values"] = labels
            self.abaqus_version_var.set(labels[0])
            if not self.abaqus_cmd_var.get().strip():
                self.abaqus_cmd_var.set("abaqus")
            self.log("No installed Abaqus command found automatically. Using manual mode.")
            return

        self.version_combo["values"] = labels

        current_cmd = self.abaqus_cmd_var.get().strip()
        matched_label = None
        for label, cmd in self.abaqus_label_to_cmd.items():
            if cmd == current_cmd:
                matched_label = label
                break

        if matched_label:
            self.abaqus_version_var.set(matched_label)
        else:
            self.abaqus_version_var.set(labels[0])
            self.abaqus_cmd_var.set(self.abaqus_label_to_cmd[labels[0]])

        if initial:
            self.log("Detected %d Abaqus command candidate(s)." % len(options))
        else:
            self.log("Refreshed Abaqus versions: %d candidate(s)." % len(options))

    def _on_version_selected(self, _event=None):
        label = self.abaqus_version_var.get().strip()
        cmd = self.abaqus_label_to_cmd.get(label, "").strip()
        if cmd:
            self.abaqus_cmd_var.set(cmd)
            self.log("Selected Abaqus command: %s" % cmd)

    def log(self, msg):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def _clear_param_widgets(self):
        for child in self.param_frame.winfo_children():
            child.destroy()
        self.param_entries = {}

    def reload_parameters(self):
        script_path = self.script_path_var.get().strip()
        if not script_path or not os.path.isfile(script_path):
            mb.showerror("Script Error", "Script path is invalid. Please choose a valid .py file.")
            return

        try:
            with open(script_path, "r") as f:
                text = f.read()
        except Exception as exc:
            mb.showerror("Read Error", "Failed to read script (%s)." % exc.__class__.__name__)
            return

        params, _start, _end = parse_parameters(text)
        if not params:
            mb.showwarning("No Parameters", "No editable parameters found in 'Global Parameters (EDIT THESE)' block.")
            return

        self.current_script_text = text
        self.current_params = params

        self._clear_param_widgets()

        ttk.Label(self.param_frame, text="Parameter", width=24).grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Label(self.param_frame, text="Value (Python expression)", width=46).grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(self.param_frame, text="Comment", width=52).grid(row=0, column=2, sticky="w", padx=4, pady=4)

        for idx, name in enumerate(sorted(params.keys()), start=1):
            info = params[name]
            val_var = tk.StringVar(value=info["value"])

            ttk.Label(self.param_frame, text=name).grid(row=idx, column=0, sticky="w", padx=4, pady=2)
            ttk.Entry(self.param_frame, textvariable=val_var, width=44).grid(row=idx, column=1, sticky="ew", padx=4, pady=2)
            ttk.Label(self.param_frame, text=info["comment"] or "", foreground="#666666").grid(
                row=idx, column=2, sticky="w", padx=4, pady=2
            )

            self.param_entries[name] = val_var

        self.param_frame.columnconfigure(1, weight=1)
        self.log("Loaded %d parameters from script." % len(params))

    def _collect_overrides(self):
        overrides = {}
        for name, var in self.param_entries.items():
            overrides[name] = var.get().strip()
        return overrides

    def _write_temp_script(self, append_tail=""):
        script_path = self.script_path_var.get().strip()
        script_dir = os.path.dirname(os.path.abspath(script_path))

        if not self.current_script_text:
            with open(script_path, "r") as f:
                self.current_script_text = f.read()

        overrides = self._collect_overrides()
        updated = apply_parameter_overrides(self.current_script_text, overrides)
        if append_tail:
            updated += append_tail

        fd_, temp_path = tempfile.mkstemp(prefix="abaqus_gui_", suffix=".py", dir=script_dir)
        os.close(fd_)
        with open(temp_path, "w") as f:
            f.write(updated)

        return temp_path

    def _run_subprocess_thread(self, cmd, cwd, temp_script, mode_name):
        try:
            self.log("[%s] Running command:" % mode_name)
            self.log("  (command hidden for privacy)")

            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                errors="replace",
            )

            for line in proc.stdout:
                self.log(line.rstrip("\r\n"))

            code = proc.wait()
            self.log("[%s] Process finished with code: %s" % (mode_name, code))
            if code == 0:
                self.log("[%s] Success." % mode_name)
            else:
                self.log("[%s] Failed. Check log above." % mode_name)

        except Exception as exc:
            self.log("[%s] Error: %s" % (mode_name, exc.__class__.__name__))
        finally:
            try:
                if os.path.isfile(temp_script):
                    os.remove(temp_script)
            except Exception as exc:
                self.log("Could not remove temp script: %s" % exc.__class__.__name__)

            self.root.after(0, self._set_buttons_state, "normal")

    def _set_buttons_state(self, state):
        self.btn_mode1.config(state=state)
        self.btn_mode2.config(state=state)

    def run_mode_open_cae(self):
        script_path = self.script_path_var.get().strip()
        if not os.path.isfile(script_path):
            mb.showerror("Script Error", "Invalid script path.")
            return

        abaqus_cmd = self.abaqus_cmd_var.get().strip()
        if not abaqus_cmd:
            mb.showerror("Command Error", "Abaqus command cannot be empty.")
            return

        try:
            temp_script = self._write_temp_script(append_tail="")
        except Exception as exc:
            mb.showerror("Prepare Error", "Failed to prepare script (%s)." % exc.__class__.__name__)
            return

        self._set_buttons_state("disabled")
        cwd = os.path.dirname(os.path.abspath(script_path))
        cmd = [abaqus_cmd, "cae", "script=%s" % temp_script]

        t = threading.Thread(
            target=self._run_subprocess_thread,
            args=(cmd, cwd, temp_script, "Mode 1"),
        )
        t.daemon = True
        t.start()

    def run_mode_export_inp(self):
        script_path = self.script_path_var.get().strip()
        if not os.path.isfile(script_path):
            mb.showerror("Script Error", "Invalid script path.")
            return

        abaqus_cmd = self.abaqus_cmd_var.get().strip()
        if not abaqus_cmd:
            mb.showerror("Command Error", "Abaqus command cannot be empty.")
            return

        out_dir = self.inp_out_var.get().strip()
        if not out_dir:
            mb.showerror("Output Error", "Please set an INP output directory.")
            return

        try:
            if not os.path.isdir(out_dir):
                os.makedirs(out_dir)
        except Exception as exc:
            mb.showerror("Output Error", "Failed to create output directory (%s)." % exc.__class__.__name__)
            return

        try:
            tail = build_inp_export_tail(out_dir)
            temp_script = self._write_temp_script(append_tail=tail)
        except Exception as exc:
            mb.showerror("Prepare Error", "Failed to prepare export script (%s)." % exc.__class__.__name__)
            return

        self._set_buttons_state("disabled")
        cwd = os.path.dirname(os.path.abspath(script_path))
        cmd = [abaqus_cmd, "cae", "noGUI=%s" % temp_script]

        t = threading.Thread(
            target=self._run_subprocess_thread,
            args=(cmd, cwd, temp_script, "Mode 2"),
        )
        t.daemon = True
        t.start()


def main():
    root = tk.Tk()
    app = AbaqusParamGui(root)
    app.log("Tips:")
    app.log("1) The GUI auto-detects available Abaqus versions at startup.")
    app.log("2) Use 'Refresh Versions' if you installed/switched Abaqus after launch.")
    app.log("3) You can still manually edit Abaqus command text if needed.")
    app.log("4) Click 'Reload Params' after changing script file.")
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
