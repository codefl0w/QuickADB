[**EDIT - 18.06.25**] Long time no see! If you're reading this, just keep in mind: QuickADB isn't dead and a new major update should arrive soon.

   ![logo](https://github.com/user-attachments/assets/70412b0d-d1f6-432e-aaf4-32e535f2f5f2) 



A simple GUI created in Python in effort to make ADB &amp; Fastboot commands faster and easier for both beginners and experts. 

## FEATURES

Built-in SDK Platform Tools version 1.0.41 to execute the commands. The terminal also starts from the same directory as the platform tools, so you can execute any command without having the folder added to your PATH.

Logs section to keep track of the output.

ADB Section to execute the most needed ADB commands, including a quick Pull/Push feature.

Fastboot section to reboot into different modes and checking getvar variables. Supports locking and unlocking the bootloader. (****Command availability varies by device manufacturer.****)

Flashing section to flash to 24 different partitions via fastboot. Supports ".img" and ".bin" files.

**[V2.0.0]** Advanced tab. Automatic driver installers, payload dumper powered by [@ssut's payload-dumper-go](https://github.com/ssut/payload-dumper-go), ADB debloater, Magisk installer and more.

**[V3.0.0]** A fully automatic GSI Flasher, wireless ADB function, auto update checker for  the tool and a super.img dumper powered by [unix3dgforce's lpunpack script](https://github.com/unix3dgforce/lpunpack)
## NOTES

**Always** keep an eye on the logs while flashing an image. If you get a faulty output, please use the terminal and flash the image manually before rebooting your device.

Kaspersky and 6 unknown vendors flag QuickADB as trojan. This is a false-positive alert. This happens because the program was compiled with PyInstaller, so it doesn't have a signature.


This software uses [ssut's payload-dumper-go](https://github.com/ssut/payload-dumper-go) for payload extraction functionality, which is licensed under the Apache License 2.0.

This software uses [unix3dgforce's lpunpack](https://github.com/unix3dgforce/lpunpack) for super image unpacking, which is licensed under the GNU Lesser General Public License v3.0 (LGPL-3.0).







[<img width="410" alt="bmc-button" src="https://github.com/user-attachments/assets/2a3f0ad2-ac4d-41b2-ae26-eade44d2dc78">](https://buymeacoffee.com/fl0w)[<img width="410" alt="xda_button" src="https://github.com/user-attachments/assets/7559f853-2ac2-4715-b3a3-9fd0c0d0abda">](https://xdaforums.com/t/tool-quickadb-a-gui-to-execute-adb-fastboot-commands.4690673/) 


![toolV3 0 0](https://github.com/user-attachments/assets/6eab4ac9-cbe5-4f24-b218-ed5f68f753f6)




