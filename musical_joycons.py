#!/usr/bin/python3
import sys
from tkinter import *
from tkinter.ttk import *
import tkinter as tk
from tkinter import filedialog
from mido1 import *
import re
import famistudio_rumble as fr
from tkinter.messagebox import *
def close():
    stop()
    root.destroy()
    sys.exit()
def check():
    if fr.get_state()==1:
        time=fr.get_pos()
        playback_l.config(text=f'{int(time/60)}:{"0" if int(time)%60<10 else ""}{int(time)%60}:{"0" if int(time*30)%30<10 else ""}{int(time*30)%30} | Pat {fr.get_pos_pattern()+1}')
        root.after(33, check)
    else:
        play_b.config(text='Play')
        if fr.get_state()==2:
            time=fr.get_pos()
            playback_l.config(text=f'{int(time/60)}:{"0" if int(time)%60<10 else ""}{int(time)%60}:{"0" if int(time*30)%30<10 else ""}{int(time*30)%30} | Pat {fr.get_pos_pattern()+1}')
            return
        playback_l.config(text='Stopped')
        mp.config(state=NORMAL)
        open_b.config(state=NORMAL)
        stop_b.config(state=DISABLED)
        for i in range(6):
            exec(f'sp{i}.config(state=NORMAL)')
        fr.all_notes_off()
        for i in range(6):
            exec(f'b{i+1}.config(state=NORMAL)')
def play_pause():
    if fr.get_state()==1:
        fr.pause()
        play_b.config(text='Play')
        stop_b.config(state=NORMAL)
        mp.config(state=NORMAL)
        fr.all_notes_off()
    else:
        fr.set_master_pitch(int(mp.get()))
        try:
            fr.play()
        except RuntimeError as e:
            showerror("Playback not possible", str(e))
        else:
            play_b.config(text='Pause')
            stop_b.config(state=NORMAL)
            mp.config(state=DISABLED)
            playback_l.config(text="0:00:00 | Pat 1")
            open_b.config(state=DISABLED)
            for i in range(6):
                exec(f'sp{i}.config(state=DISABLED)')
            check()
            for i in range(6):
                exec(f'b{i+1}.config(state=DISABLED)')
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
                file_l.config(text=file_path)
                root.filename=file_path
                play_b.config(state=NORMAL)
        else:
            showerror("Invalid file", "The file does not seem to be in the FamiStudio text format.")
def stop():
    fr.stop()
    play_b.config(text='Play')
    playback_l.config(text='Stopped')
    mp.config(state=NORMAL)
    stop_b.config(state=DISABLED)
    for i in range(6):
        exec(f'sp{i}.config(state=NORMAL)')
    open_b.config(state=NORMAL)
    for i in range(6):
        exec(f'b{i+1}.config(state=NORMAL)')
def test_master_pitch(x):
    try:
        fr.test_master_pitch(x)
    except (ValueError, IndexError):
        showerror("No controller", f"There is no controller assigned to channel {x+1}.")
    except RuntimeError:
        showerror("No controller", "There are no JoyCons or ProCons connected.")
    except OSError:
        showerror("Operation failed", "The command has failed. Check if the controller has shut down unexpectedly.")
def set_channel():
    for i in range (6):
        exec(f'fr.set_channel({i+1}, int(sp{i}.get()[0]))')
root=Tk()
fr.set_loop(False)
root.filename=''
root.resizable(False, False)
root.title("Musical JoyCons Revamped")
root.protocol("WM_DELETE_WINDOW", close)
Label(root, text="Musical JoyCons Revamped").pack()
frame1=LabelFrame(root, text="Sequencer playback")
frame1.pack()
frame2=Frame(frame1)
frame2.pack(anchor='w')
frame3=Frame(frame1)
frame3.pack(pady=5, anchor='w')
open_b=Button(frame2, text="Open FamiStudio text file", command=open_file)
open_b.pack(side=LEFT)
file_l=Label(frame2, text="No file opened")
file_l.pack(side=LEFT, padx=5)
play_b=Button(frame3, text="Play", command=play_pause, state=DISABLED)
play_b.pack(side=LEFT)
stop_b=Button(frame3, text="Stop", command=stop, state=DISABLED)
stop_b.pack(side=LEFT, padx=5)
playback_l=Label(frame3, text="-:--:-- | Pat -")
playback_l.pack(side=LEFT, padx=5)
frame4=LabelFrame(frame1, text="Playback options")
frame4.pack()
frame5=Frame(frame4)
frame5.pack(pady=10, anchor='w')
Label(frame5, text="A₄=").pack(side=LEFT)
mp=tk.Scale(frame5, from_=410, to=480, orient=HORIZONTAL, length=300, command=lambda v: fr.set_master_pitch(int(v)))
mp.pack(side=LEFT, padx=5)
mp.set(440)
frame10=LabelFrame(root, text="JoyCons")
frame10.pack()
Button(frame10, text='All notes OFF', command=fr.all_notes_off).pack()
Label(frame10, text="JoyCon Mapping:").pack()
for i in range(3):
    exec(f'frame{i+13}=Frame(frame10)')
    exec(f'frame{i+13}.pack()')
    exec(f'Label(frame{i+13}, text="Actuator {i*2+1}:").pack(side=LEFT)')
    exec(f'sp{i*2}=Spinbox(frame{i+13}, from_=1, to=6, width=3, state="readonly", command=set_channel)')
    exec(f'sp{i*2}.pack(side=LEFT, padx=5)')
    exec(f'sp{i*2}.set({i*2+1})')
    exec(f'Label(frame{i+13}, text="Actuator {i*2+2}:").pack(side=LEFT, padx=5)')
    exec(f'sp{i*2+1}=Spinbox(frame{i+13}, from_=1, to=6, width=3, state="readonly", command=set_channel)')
    exec(f'sp{i*2+1}.pack(side=LEFT, padx=5)')
    exec(f'sp{i*2+1}.set({i*2+2})')
frame11=Frame(frame10)
frame11.pack()
frame12=LabelFrame(frame11, text='Test: Play test sound')
frame12.pack()
ch_l=("Square 1", "Square 2", "Triangle", "Noise", "MMC5 S1", "MMC5 S2")
for i in range(6):
    exec(f'b{i+1}=Button(frame12, text="{i+1} ({ch_l[i]})", command=lambda x=i: test_master_pitch(x))')
    exec(f'b{i+1}.pack(side=LEFT, padx=5)')
root.mainloop()
