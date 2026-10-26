import socket
import pickle
import struct
import cv2
import pyautogui
import numpy as np

# নেটওয়ার্ক কনফিগারেশন
HOST = '0.0.0.0'  # আপনার লোকাল আইপি অ্যাড্রেস এখানে দিন
PORT = 9999

server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.bind((HOST, PORT))
server_socket.listen(5)
print(f"সার্ভার চালু হয়েছে... পোর্ট {PORT}-এ কানেকশনের জন্য অপেক্ষা করছে।")

client_socket, addr = server_socket.accept()
print(f"{addr} এর সাথে কানেকশন সফল হয়েছে!")

try:
    while True:
        # স্ক্রিনশট নেওয়া
        img = pyautogui.screenshot()
        
        # ইমেজটিকে numpy অ্যারে এবং তারপর OpenCV ফরম্যাটে রূপান্তর
        frame = np.array(img)
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # ভিডিও সাইজ কমানোর জন্য ফ্রেম রিসাইজ করা (অপশনাল কিন্তু পারফরম্যান্সের জন্য ভালো)
        frame = cv2.resize(frame, (960, 540))
        
        # ডাটা সিরিয়ালাইজ (প্যাকেট) করা
        data = pickle.dumps(frame)
        message_size = struct.pack("L", len(data))
        
        # ডাটা পাঠানো
        client_socket.sendall(message_size + data)
        
except Exception as e:
    print(f"ত্রুটি: {e}")
finally:
    client_socket.close()
    server_socket.close()

