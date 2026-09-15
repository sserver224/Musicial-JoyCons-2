#!/usr/bin/python3
import sys
from customtkinter import *
from tkinter import filedialog
import re
import famistudio_rumble as fr
from tkinter.messagebox import *
from idlelib.tooltip import Hovertip
def close():
    stop()
    root.destroy()
    sys.exit()
def check():
    if fr.get_state()==1:
        time=fr.get_pos()
        playback_l.configure(text=f'{int(time/60)}:{"0" if int(time)%60<10 else ""}{int(time)%60}:{"0" if int(time*30)%30<10 else ""}{int(time*30)%30} | Pat {fr.get_pos_pattern()+1}')
        root.after(33, check)
    else:
        play_b.configure(text='Play')
        if fr.get_state()==2:
            time=fr.get_pos()
            playback_l.configure(text=f'{int(time/60)}:{"0" if int(time)%60<10 else ""}{int(time)%60}:{"0" if int(time*30)%30<10 else ""}{int(time*30)%30} | Pat {fr.get_pos_pattern()+1}')
            return
        playback_l.configure(text='Stopped')
        mp.configure(state=NORMAL)
        open_b.configure(state=NORMAL)
        stop_b.configure(state=DISABLED)
        for i in range(6):
            exec(f'sp{i}.configure(state=NORMAL)')
        fr.all_notes_off()
        for i in range(6):
            exec(f'b{i+1}.configure(state=NORMAL)')
def play_pause():
    if fr.get_state()==1:
        fr.pause()
        play_b.configure(text='Play')
        stop_b.configure(state=NORMAL)
        mp.configure(state=NORMAL)
        fr.all_notes_off()
    else:
        fr.set_master_pitch(int(mp.get()))
        try:
            fr.play()
        except RuntimeError as e:
            showerror("Playback not possible", str(e))
        else:
            play_b.configure(text='Pause')
            stop_b.configure(state=NORMAL)
            mp.configure(state=DISABLED)
            playback_l.configure(text="0:00:00 | Pat 1")
            open_b.configure(state=DISABLED)
            for i in range(6):
                exec(f'sp{i}.configure(state=DISABLED)')
            check()
            for i in range(6):
                exec(f'b{i+1}.configure(state=DISABLED)')
def open_file():
    file_path = filedialog.askopenfilename(
        title="Open FamiStudio Text",
        initialdir=".",
        filetypes=[("FamiStudio Text", "*.txt")]
        )
    if file_path:
        pattern = (
            r'^Project Version="([^"]*)" '
            r'TempoMode="([^"]*)" '
            r'Name="([^"]*)" '
        )
        with open(file_path, "r") as f:
            first_line = f.readline().strip()

        match = re.match(pattern, first_line)

        if match:
            try:
                fr.load(file_path)
            except FileNotFoundError as e:
                showerror("Load failed", str(e))
            except ValueError as e:
                if "requires a FamiStudio-tempo text export" in str(e):
                    showerror("Load failed", "This program requires FamiStudio tempo-mode data.")
            else:
                file_l.configure(text=file_path)
                root.filename=file_path
                play_b.configure(state=NORMAL)
        else:
            showerror("Invalid file", "The file does not seem to be in the FamiStudio text format.")
def stop():
    fr.stop()
    play_b.configure(text='Play')
    playback_l.configure(text='Stopped')
    mp.configure(state=NORMAL)
    stop_b.configure(state=DISABLED)
    for i in range(6):
        exec(f'sp{i}.configure(state=NORMAL)')
    open_b.configure(state=NORMAL)
    for i in range(6):
        exec(f'b{i+1}.configure(state=NORMAL)')
def test_master_pitch(x):
    try:
        fr.test_master_pitch(x)
    except (ValueError, IndexError):
        showerror("No controller", f"There is no controller assigned to channel {x+1}.")
    except RuntimeError:
        showerror("No controller", "There are no JoyCons or ProCons connected.")
    except OSError:
        showerror("Operation failed", "The command has failed. Check if the controller has shut down unexpectedly.")
def set_channel(x):
    for i in range (6):
        exec(f'fr.set_channel({i+1}, int(sp{i}.get()))')
def set_pitch(v):
    fr.set_master_pitch(v)
    cal_l.configure(text=f"A₄={v} Hz")
def get_battery(index):
    try:
        raw = fr.get_battery_level(index)
    except IndexError:
        showerror("Controller Info", f"No controller is in slot {index}.")
    except RuntimeError:
        showerror("No controller", "There are no JoyCons or ProCons connected.")
    except OSError:
        showerror("Operation failed", "The command has failed. Check if the controller has shut down unexpectedly.")
    else:
        battery = (raw >> 4) & 0x0F
        conn_info = raw & 0x0F

        battery_levels = {
            8: "full",
            6: "medium",
            4: "low",
            2: "critical",
            0: "empty",
        }

        level = battery_levels.get(battery, "unknown")

        controller_type = (conn_info >> 1) & 0x03
        powered = bool(conn_info & 0x01)

        if controller_type == 0:
            conn = "Pro/Charging Grip"
        elif controller_type == 3:
            conn = "Joy-Con"
        else:
            conn = "unknown"

        if powered:
            conn += ", Wired"
        
        showinfo("Controller Info", f"Logical actuator {index}:\nBattery: {level}\nConnection info: {conn}")
root=CTk()
fr.set_loop(False)
root.filename=''
root.resizable(False, False)
root.title("Musical JoyCons Revamped")
root.protocol("WM_DELETE_WINDOW", close)
CTkLabel(root, text="Musical JoyCons Revamped").pack()
frame1=CTkFrame(root)
frame1.pack()
frame2=CTkFrame(frame1)
frame2.pack(anchor='w')
frame3=CTkFrame(frame1)
frame3.pack(pady=5, anchor='w')
open_b=CTkButton(frame2, text="Open FamiStudio text file", command=open_file)
open_b.pack(side=LEFT)
file_l=CTkLabel(frame2, text="No file opened")
file_l.pack(side=LEFT, padx=5)
play_b=CTkButton(frame3, text="Play", command=play_pause, state=DISABLED)
play_b.pack(side=LEFT)
stop_b=CTkButton(frame3, text="Stop", command=stop, state=DISABLED)
stop_b.pack(side=LEFT, padx=5)
playback_l=CTkLabel(frame3, text="-:--:-- | Pat -")
playback_l.pack(side=LEFT, padx=5)
frame4=CTkFrame(frame1)
frame4.pack()
frame5=CTkFrame(frame4)
frame5.pack(pady=10, anchor='w')
cal_l=CTkLabel(frame5, text="A₄=440 Hz")
cal_l.pack(side=LEFT)
mp=CTkSlider(frame5, from_=410, to=480, command=lambda v: set_pitch(int(v)))
mp.pack(side=LEFT, padx=5)
mp.set(440)
frame10=CTkFrame(root)
frame10.pack()
CTkButton(frame10, text='All notes OFF', command=fr.all_notes_off).pack()
CTkLabel(frame10, text="JoyCon Mapping:").pack()
ch_l=("Square 1", "Square 2", "Triangle", "Noise", "MMC5 S1", "MMC5 S2")
for i in range(3):
    exec(f'frame{i+13}=CTkFrame(frame10)')
    exec(f'frame{i+13}.pack()')
    exec(f'f{i}=CTkLabel(frame{i+13}, text="Actuator {i*2+1}:", cursor="hand2")')
    exec(f'f{i}.pack(side=LEFT)')
    exec(f'f{i}.bind("<Button-1>", lambda x: get_battery({i*2+1}))')
    exec(f'Hovertip(f{i}, "Click to view connection info")') 
    exec(f'sp{i*2}=CTkOptionMenu(frame{i+13}, values=("1", "2", "3", "4", "5", "6"), command=set_channel)')
    exec(f'sp{i*2}.pack(side=LEFT, padx=5)')
    exec(f'sp{i*2}.set({i*2+1})')
    exec(f'f{i*2}=CTkLabel(frame{i+13}, text="Actuator {i*2+2}:", cursor="hand2")')
    exec(f'f{i*2}.pack(side=LEFT, padx=5)')
    exec(f'Hovertip(f{i*2}, "Click to view connection info")') 
    exec(f'f{i*2}.bind("<Button-1>", lambda x: get_battery({i*2+2}))')
    exec(f'sp{i*2+1}=CTkOptionMenu(frame{i+13}, values=("1", "2", "3", "4", "5", "6"), command=set_channel)')
    exec(f'sp{i*2+1}.pack(side=LEFT, padx=5)')
    exec(f'sp{i*2+1}.set({i*2+2})')
frame11=CTkFrame(frame10)
frame11.pack()
CTkLabel(frame11, text="Test: Play test tone").pack()
frame12=CTkFrame(frame11)
frame12.pack()
for i in range(6):
    exec(f'b{i+1}=CTkButton(frame12, text="{i+1} ({ch_l[i]})", command=lambda x=i: test_master_pitch(x))')
    exec(f'b{i+1}.pack(side=LEFT, padx=5)')
root.mainloop()
