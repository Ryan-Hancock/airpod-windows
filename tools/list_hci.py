"""List USB Bluetooth HCI dongles with the Bumble path moniker for each (Windows/libusb)."""
from bumble.transport.usb import load_libusb
import usb1

load_libusb()
with usb1.USBContext() as ctx:
    for d in ctx.getDeviceIterator(skip_on_error=True):
        cls = (d.getDeviceClass(), d.getDeviceSubClass(), d.getDeviceProtocol())
        if cls != (0xE0, 0x01, 0x01):
            continue
        path = f"{d.getBusNumber()}-{'.'.join(map(str, d.getPortNumberList()))}"
        try:
            h = d.open(); h.close(); access = "openable (WinUSB)"
        except usb1.USBError as e:
            access = f"not openable ({e.__class__.__name__}) - owned by Windows driver?"
        print(f"{d.getVendorID():04X}:{d.getProductID():04X}  path={path}  {access}")
