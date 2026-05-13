import cv2

FILE_PNG_A = 'sample/qrcode_kura.png'
FILE_PNG_B = 'sample/qrcode_ni.png'
FILE_PNG_AB = 'sample/qrcode_concat.png'

im1 = cv2.imread(FILE_PNG_A)
im2 = cv2.imread(FILE_PNG_B)

im_h = cv2.hconcat([im1, im2])
cv2.imwrite(FILE_PNG_AB, im_h)

# cv2.imshow('image', im_h)
# cv2.waitKey(0)
