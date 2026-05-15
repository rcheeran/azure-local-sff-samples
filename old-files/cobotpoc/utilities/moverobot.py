from time import sleep, time
from pymycobot.mycobot280 import MyCobot280

robotPort = "/dev/ttyACM0"
robotBaud = 115200
ROBOT = MyCobot280(robotPort, robotBaud)
# Wait briefly to ensure connection is established
sleep(2)

# Lean back
targetAngles = [-30, 45, -45, 0, 30, -45]
ROBOT.send_angles(targetAngles, 50)
sleep(2)

# Stand up straight
targetAngles = [0, 0, 0, 0, 0, 0]
ROBOT.send_angles(targetAngles, 50)
sleep(2)

# Lean back
targetAngles = [-30, 45, -45, 0, 30, -45]
ROBOT.send_angles(targetAngles, 50)
sleep(2)

# Stand up straight
targetAngles = [0, 0, 0, 0, 0, 0]
ROBOT.send_angles(targetAngles, 50)
sleep(2)