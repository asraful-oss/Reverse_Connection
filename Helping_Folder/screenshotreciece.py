import socket
import pickle
import struct
import cv2

# শেয়ারার পিসির আইপি এবং পোর্ট
SERVER_IP = '192.168.X.X'  # এখানে Sender-এর আসল আইপি বসান
PORT = 9999

client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client_socket.connect((SERVER_IP, PORT))
print(f"[সংযোগ] সফলভাবে {SERVER_IP} এর সাথে সংযুক্ত হয়েছে।")
print("[ইনফো] স্ক্রিন দেখা বন্ধ করতে 'Shared Screen' উইন্ডোতে ক্লিক করে কিবোর্ডের 'q' চাপুন।\n")

data = b""
payload_size = struct.calcsize("L")

try:
    while True:
        # মেসেজ সাইজ ডাটা রিসিভ করা
        while len(data) < payload_size:
            data += client_socket.recv(4096)
            
        packed_msg_size = data[:payload_size]
        data = data[payload_size:]
        msg_size = struct.unpack("L", packed_msg_size)[0]
        
        # পুরো ফ্রেমের ডাটা রিসিভ করা
        while len(data) < msg_size:
            data += client_socket.recv(4096)
            
        frame_data = data[:msg_size]
        data = data[msg_size:]
        
        # ডাটা ডিকোড করে ফ্রেমে রূপান্তর
        frame = pickle.loads(frame_data)
        
        # আলাদা একটি পপ-আপ উইন্ডোতে স্ক্রিন প্রদর্শন
        cv2.imshow("Shared Screen", frame)
        print("[+] ফ্রেম রিসিভ হচ্ছে...", end="\r")
        
        # 'q' চাপলে উইন্ডো বন্ধ হয়ে যাবে
        if cv2.waitKey(1) & 0xFF == ord('q'):
            print("\n[!] ব্যবহারকারী দ্বারা কানেকশন বন্ধ করা হয়েছে।")
            break
            
except Exception as e:
    print(f"\n[বিচ্ছিন্ন] কানেকশন বন্ধ হয়েছে: {e}")
finally:
    cv2.destroyAllWindows()
    client_socket.close()

