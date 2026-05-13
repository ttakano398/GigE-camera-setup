import pyqrcode
import os

os.makedirs("sample")

FILE_PNG_A = 'sample/qrcode_kura.png'
FILE_PNG_B = 'sample/qrcode_ni.png'

# QRコード作成
code = pyqrcode.create('https://www.kurasushi.co.jp/', error='L', version=3, mode='binary')
code.png(FILE_PNG_A, scale=5, module_color=[0, 0, 0, 128], background=[255, 255, 255])

# QRコード作成
code = pyqrcode.create('https://newinov.com/', error='L', version=3, mode='binary')
code.png(FILE_PNG_B, scale=5, module_color=[0, 0, 0, 128], background=[255, 255, 255])
