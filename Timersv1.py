import pyautogui
import time
import tkinter as tk
import pygetwindow as gw
import threading


# === GLOBALS ===
running = False
cancel_event = threading.Event()
start_time_sec = 0
end_time_sec = 0


# === STATUS BAR FUNCTION ===
def show_status(text, color="green", timeout=3000):
    """
    Show temporary message in status bar.
    Auto-clears after timeout.
    """
    label_message.config(text=text, fg=color)
    if timeout > 0:
        # Cancel any previous timeout
        if hasattr(show_status, "after_id"):
            root.after_cancel(show_status.after_id)
        # Schedule reset
        show_status.after_id = root.after(timeout, lambda: label_message.config(text="Ready", fg="green"))


# === ACTIVATE OBS (RELIABLE) ===
def activate_obs():
    prefix = entry_window.get().strip() or "OBS"
    windows = gw.getAllTitles()
    for title in windows:
        if title.lower().startswith(prefix.lower()):
            win = gw.getWindowsWithTitle(title)[0]
            win.activate()
            time.sleep(1)
            show_status("Activated OBS", "green", 2000)
            return True
    show_status("Error", f"No window starting with '{prefix}' found.")
    return False

# === SEND HOTKEY ===
def send_keys(keys):
    keys = [k.strip().lower() for k in keys.split('+')]
    print(f"Sending: {' + '.join(keys).upper()}")
    pyautogui.hotkey(*keys)


# === MAIN TIMER LOGIC ===
def run_session():
    global running, start_time_sec, end_time_sec
    if running:
        show_status("Session already running!", "orange")
        return

    # === VALIDATE INPUT ===
    try:
        h1 = int(entry_start_h.get() or 0)
        m1 = int(entry_start_m.get() or 0)
        s1 = int(entry_start_s.get() or 0)
        start_time_sec = h1 * 3600 + m1 * 60 + s1

        h2 = int(entry_end_h.get() or 0)
        m2 = int(entry_end_m.get() or 0)
        s2 = int(entry_end_s.get() or 0)
        end_time_sec = h2 * 3600 + m2 * 60 + s2

        start_shortcut = entry_start_shortcut.get().strip()
        stop_shortcut = entry_stop_shortcut.get().strip()

        if start_time_sec >= end_time_sec:
            raise ValueError("Start time must be LESS than End time!")
        if not start_shortcut or not stop_shortcut:
            raise ValueError("Both shortcuts required!")
    except ValueError as e:
        show_status(f"Invalid: {e}", "red")
        return

    # === START SESSION ===
    running = True
    cancel_event.clear()
    btn_start.config(state="disabled", text="Running...")
    label_status.config(text="Starting in...", fg="orange")
    label_countdown.config(text="")

    # === COUNTDOWN THREAD ===
    def countdown():
        global running
        current = 0
        total = end_time_sec

        while current <= end_time_sec and not cancel_event.is_set():
            h, rem = divmod(total - current, 3600)
            m, s = divmod(rem, 60)
            root.after(0, lambda h=h, m=m, s=s, c=current: [
                label_countdown.config(text=f"{h:02d}:{m:02d}:{s:02d}"),
                label_status.config(text="Recording..." if c >= start_time_sec else "Starting soon...", fg="blue" if c >= start_time_sec else "orange")
            ])

            # === START RECORDING ===
            if current == start_time_sec:
                if activate_obs():
                    activate_obs()
                    send_keys(start_shortcut)
                    show_status("RECORDING STARTED!", "lime", 3000)
                else:
                    show_status("Failed to start OBS", "red")

            # === STOP RECORDING ===
            if current == end_time_sec:
                if activate_obs():
                    activate_obs()
                    send_keys(stop_shortcut)
                    show_status("RECORDING STOPPED!", "red", 3000)
                break

            time.sleep(1)
            current += 1

        # === RESET UI ===
        def reset_ui():
            btn_start.config(state="normal", text="Start Session")
            label_status.config(text="Ready", fg="green")
            label_countdown.config(text="")
            globals()['running'] = False

        root.after(0, reset_ui)

    threading.Thread(target=countdown, daemon=True).start()


# === CANCEL SESSION ===
def cancel_session():
    global running
    if running:
        cancel_event.set()
        show_status("Session CANCELLED", "orange", 2000)
        btn_start.config(state="normal", text="Start Session")
        label_status.config(text="Cancelled", fg="red")
        label_countdown.config(text="")
        running = False
    else:
        show_status("No session running", "gray", 1500)


# === GUI SETUP ===
root = tk.Tk()
root.title("Shortcut Timer")
root.geometry("520x380")
root.resizable(False, False)

# === WINDOW PREFIX ===
tk.Label(root, text="Window Prefix:").grid(row=0, column=0, columnspan=2, padx=10, pady=8, sticky="w")
entry_window = tk.Entry(root, width=25)
entry_window.grid(row=0, column=2, columnspan=2, padx=10, pady=8, sticky="w")
entry_window.insert(0, "OBS")

# === START TIME ===
tk.Label(root, text="START AFTER", font=("Arial", 9, "bold")).grid(row=1, column=0, columnspan=4, pady=(15,0), sticky="w")
frame_start = tk.Frame(root)
frame_start.grid(row=2, column=0, columnspan=4, pady=5, padx=10, sticky="w")
tk.Label(frame_start, text="H:").grid(row=0, column=0); entry_start_h = tk.Entry(frame_start, width=4); entry_start_h.grid(row=0, column=1, padx=2)
tk.Label(frame_start, text="M:").grid(row=0, column=2); entry_start_m = tk.Entry(frame_start, width=4); entry_start_m.grid(row=0, column=3, padx=2)
tk.Label(frame_start, text="S:").grid(row=0, column=4); entry_start_s = tk.Entry(frame_start, width=4); entry_start_s.grid(row=0, column=5, padx=2)
entry_start_h.insert(0, "0"); entry_start_m.insert(0, "0"); entry_start_s.insert(0, "5")

tk.Label(root, text="Start Shortcut:").grid(row=3, column=0, padx=12, pady=5, sticky="w")
entry_start_shortcut = tk.Entry(root, width=20)
entry_start_shortcut.grid(row=3, column=1, columnspan=2, padx=10, sticky="w")
entry_start_shortcut.insert(0, "ctrl+alt+b")

# === END TIME ===
tk.Label(root, text="STOP AFTER", font=("Arial", 9, "bold")).grid(row=4, column=0, columnspan=4, pady=(15,0), sticky="w")
frame_end = tk.Frame(root)
frame_end.grid(row=5, column=0, columnspan=4, pady=5, padx=10, sticky="w")
tk.Label(frame_end, text="H:").grid(row=0, column=0); entry_end_h = tk.Entry(frame_end, width=4); entry_end_h.grid(row=0, column=1, padx=2)
tk.Label(frame_end, text="M:").grid(row=0, column=2); entry_end_m = tk.Entry(frame_end, width=4); entry_end_m.grid(row=0, column=3, padx=2)
tk.Label(frame_end, text="S:").grid(row=0, column=4); entry_end_s = tk.Entry(frame_end, width=4); entry_end_s.grid(row=0, column=5, padx=2)
entry_end_h.insert(0, "0"); entry_end_m.insert(0, "0"); entry_end_s.insert(0, "15")

tk.Label(root, text="Stop Shortcut:").grid(row=6, column=0, padx=12, pady=5, sticky="w")
entry_stop_shortcut = tk.Entry(root, width=20)
entry_stop_shortcut.grid(row=6, column=1, columnspan=2, padx=10, sticky="w")
entry_stop_shortcut.insert(0, "ctrl+alt+n")

# === CONTROL BUTTONS ===
btn_start = tk.Button(root, text="Start Session", font=("Arial", 10, "bold"), bg="#4CAF50", fg="white", command=run_session)
btn_start.grid(row=7, column=0, columnspan=2, pady=15, padx=10, sticky="we")

tk.Button(root, text="CANCEL", fg="red", font=("Arial", 9, "bold"), command=cancel_session).grid(row=7, column=2, pady=15, padx=5)

label_status = tk.Label(root, text="Ready", fg="green", font=("Arial", 10))
label_status.grid(row=7, column=3, pady=15)

label_countdown = tk.Label(root, text="", font=("Arial", 18, "bold"), fg="blue")
label_countdown.grid(row=8, column=0, columnspan=4, pady=10)

# === STATUS BAR ===
status_frame = tk.Frame(root, relief="sunken", bd=1)
status_frame.grid(row=9, column=0, columnspan=4, sticky="we", padx=10, pady=(5,10))
label_message = tk.Label(status_frame, text="Ready", fg="green", font=("Arial", 9), anchor="w")
label_message.pack(fill="x", padx=5, pady=2)


# === START GUI ===
root.mainloop()
