## Prerequisites

You need:

- Computer with:
  - Nvidia GPU (developed and tested on NVIDIA RTX 3050 8 GB and NVIDIA RTX 2000E Ada Generation — 16 GB VRAM, compute capability 8.9, driver 580.105.08. Single GPU)
  - [Azure Local (Linux)](https://learn.microsoft.com/en-us/azure/azure-local/small-form-factor/)

- Peripherals:
  - [MyCobot 280 M5](https://shop.elephantrobotics.com/collections/mycobot-280/products/mycobot-worlds-smallest-and-lightest-six-axis-collaborative-robot) 
  - UAC-compliant USB microphone
  - UVC-compliant USB camera

## Setup your hardware

- Plug-in the peripherals to the machine - the USB microphone and the USB camera.
- Plug-in the robot to the machine -  see elephant robotics docs for [myCobot 280 M5](https://docs.elephantrobotics.com/docs/mycobot_280_m5_en/4-SupportAndService/9.Troubleshooting/9.4-first-time-self-check.html) 

## Setup your machine

- Follow the instructions mentioned in [Azure Local (Linux)](https://learn.microsoft.com/en-us/azure/azure-local/small-form-factor/) docs to ensure that your machine is registered with Azure, connected and the machine status shows as 'Provisioned'. 
- Save the SSH private key thats downloaded during the deployment in a known location on your work computer.


## Next steps

- [Connect to your machine](connect.md)

