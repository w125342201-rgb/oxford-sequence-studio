"""Exercise official nidaqmx descriptors; native driver boundary is a stub.
Requires nidaqmx==1.6.0. This is NOT hardware verification.
"""
import json
from importlib.metadata import version
from types import SimpleNamespace
from nidaqmx.task._in_stream import InStream
from nidaqmx.task.channels import AIChannel

class NativeBoundary:
    def __init__(self):
        self.calls=[]
    def get_read_attribute_bool(self, handle, attribute):
        assert attribute == 0x2174
        self.calls.append('exists')
        return True
    def get_read_attribute_string(self, handle, attribute, buffer_size):
        assert attribute == 0x2175 and self.calls == ['exists']
        self.calls.append('channels')
        return 'Dev1/ai2'

native=NativeBoundary()
stream=InStream(SimpleNamespace(_handle=1),native)
# Only buffer allocation depends on the task's native channel enumeration.
original=InStream.get_channels_buffer_size
try:
    InStream.get_channels_buffer_size=lambda self:256
    assert stream.overloaded_chans_exist is True
    assert stream.overloaded_chans == ['Dev1/ai2']
finally:
    InStream.get_channels_buffer_size=original
assert native.calls == ['exists','channels']
class RangeBoundary:
    def __init__(self):self.calls=[]
    def get_chan_attribute_double(self,handle,name,attribute):
        assert handle==1 and name=='Dev1/ai0'
        self.calls.append(attribute)
        return {0x17dd:0.316227766,0x17de:-0.316227766,0x1818:30.}[attribute]
range_native=RangeBoundary()
channel=AIChannel(1,'Dev1/ai0',range_native)
assert channel.ai_min==-0.316227766
assert channel.ai_max==0.316227766
assert channel.ai_gain==30.
assert range_native.calls==[0x17de,0x17dd,0x1818]
print(json.dumps(dict(nidaqmx_version=version('nidaqmx'),
    official_descriptors='passed',native_calls=native.calls,
    range_readback_descriptors='passed',range_attribute_ids=range_native.calls,
    hardware_test=False),indent=2))
