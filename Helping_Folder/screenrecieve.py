import socket
import pickle
import struct
import cv2

# শেয়ারার পিসির আইপি এবং পোর্ট
SERVER_IP = '192.168.X.X'  # শেয়ারারের আসল আইপি এখানে বসান
PORT = 9999

client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client_socket.connect((SERVER_IP, PORT))
print("Shared PC connected")

data = b""
payload_size = struct.calcsize("L")

try:
    while True:
        # মেসেজের সাইজ রিসিভ করা
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
        
        # স্ক্রিন উইন্ডোতে দেখানো
        cv2.imshow("Shared Screen", frame)
        
        # 'q' চাপলে স্ক্রিন শেয়ার বন্ধ হয়ে যাবে
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
            
except Exception as e:
    print(f"Connection lost: {e}")
finally:
    cv2.destroyAllWindows()
    client_socket.close()

