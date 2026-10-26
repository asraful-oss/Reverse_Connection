import socket
import pickle
import struct
import cv2
import pyautogui
import numpy as np

# নেটওয়ার্ক কনফিগারেশন
HOST = '0.0.0.0'  
PORT = 9999

server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.bind((HOST, PORT))
server_socket.listen(5)
print(f"[সার্ভার] পোর্ট {PORT}-এ কানেকশনের জন্য অপেক্ষা করছে...")
print("[ইনফো] স্ক্রিনে কোনো ইফেক্ট দেখা যাবে না, প্রসেসটি টার্মিনালে মনিটর করুন।\n")

client_socket, addr = server_socket.accept()
print(f"[সংযোগ] {addr} এর সাথে সফলভাবে কানেক্টেড হয়েছে!\n")

try:
    while True:
        # ডিসপ্লেতে কোনো ইফেক্ট ছাড়া নীরবে স্ক্রিনশট নেওয়া
        img = pyautogui.screenshot()
        
        # ইমেজ প্রসেসিং
        frame = np.array(img)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # পারফরম্যান্স ভালো রাখার জন্য ফ্রেম সাইজ কিছুটা ছোট করা (অপোনাল)
        frame = cv2.resize(frame, (960, 540))
        
        # ডাটা প্যাকেজিং (সিরিয়ালাইজেশন)
        data = pickle.dumps(frame)
        message_size = struct.pack("L", len(data))
        
        # রিসিভারের কাছে ডাটা পাঠানো
        client_socket.sendall(message_size + data)
        print("[+] ফ্রেম পাঠানো হয়েছে...", end="\r")
        
except Exception as e:
    print(f"\n[ত্রুটি] সমস্যা হয়েছে: {e}")
finally:
    client_socket.close()
    server_socket.close()
    print("\n[বন্ধ] সার্ভার বন্ধ করা হয়েছে।")

