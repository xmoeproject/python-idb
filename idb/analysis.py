import types
import struct
import logging
import binascii
import datetime
import itertools
from collections import namedtuple

import vstruct
from vstruct.primitives import v_str
from vstruct.primitives import v_bytes
from vstruct.primitives import v_uint8
from vstruct.primitives import v_uint32

import idb
import idb.netnode


logger = logging.getLogger(__name__)


def as_unix_timestamp(buf):
    '''
    parse unix timestamp bytes into a timestamp.
    '''
    q = struct.unpack_from("<I", buf, 0x0)[0]
    return datetime.datetime.utcfromtimestamp(q)


def as_md5(buf):
    '''
    parse raw md5 bytes into a hex-formatted string.
    '''
    return binascii.hexlify(buf).decode('ascii')


def cast(buf, V):
    '''
    apply a vstruct class to a sequence of bytes.

    Args:
        buf (bytes): the bytes to parse.
        V (type[vstruct.VStruct]): the vstruct class.

    Returns:
        V: the parsed instance of V.

    Example::

        s = cast(buf, Stat)
        assert s.gid == 0x1000
    '''
    v = V()
    v.vsParse(buf)
    return v


def as_cast(V):
    '''
    create a partial function that casts buffers to the given vstruct.

    Args:
        V (type[vstruct.VStruct]): the vstruct class.

    Returns:
        callable[bytes]->V: the function that parses buffers into V instances.

    Example::

        S = as_cast(Stat)
        s = S(buf)
        assert s.gid == 0x1000
    '''
    def inner(buf):
        return cast(buf, V)
    return inner


def unpack_dd(buf, offset=0):
    '''
    IDA-specific data packing format.

    via: https://github.com/williballenthin/pyidbutil/blob/de12af8a1c32a36a5daac591f4cc5a17fa9496da/idblib.py#L161
    '''
    buf = buf[offset:]
    header = buf[0]
    if header & 0x80 == 0:
        return header, 1
    elif header & 0xC0 != 0xC0:
        return ((header & 0x7F) << 8) + buf[1], 2
    else:
        if header & 0xE0 == 0xE0:
            hi = (buf[1] << 8) + buf[2]
            low = (buf[3] << 8) + buf[4]
            size = 5
        else:
            hi = (((header & 0x3F) << 8) + buf[1])
            low = (buf[2] << 8) + buf[3]
            size = 4
        return (hi << 16) + low, size


def unpack_dds(buf):
    offset = 0
    while offset < len(buf):
        val, size = unpack_dd(buf, offset=offset)
        yield val
        offset += size


Field = namedtuple('Field', ['name', 'tag', 'index', 'cast'])
# namedtuple default args.
# via: https://stackoverflow.com/a/18348004/87207
Field.__new__.__defaults__ = (None,) * len(Field._fields)


class IndexType:
    def __init__(self, name):
        self.name = name

    def str(self):
        return self.name.upper()

ALL = IndexType('all')
ADDRESSES = IndexType('addresses')
NUMBERS = IndexType('numbers')
NODES = IndexType('nodes')

VARIABLE_INDEXES = (ALL, ADDRESSES, NUMBERS, NODES)


class _Analysis(object):
    '''
    this is basically a metaclass for analyzers of IDA Pro netnode namespaces (named nodeid).
    provide set of fields, and parse them from netnodes (nodeid, tag, and optional index) when accessed.
    '''
    def __init__(self, db, nodeid, fields):
        self.idb = db
        self.nodeid = nodeid
        self.netnode = idb.netnode.Netnode(db, nodeid)
        self.fields = fields

        self._fields_by_name = {f.name: f for f in self.fields}

    def _is_address(self, index):
        '''
        does the given index fall within a segment?
        '''
        try:
            self.idb.id1.get_segment(index)
            return True
        except KeyError:
            return False

    def _is_node(self, index):
        '''
        does the index look like a raw nodeid?
        '''
        if self.idb.wordsize == 4:
            return index & 0xFF000000 == 0xFF000000
        elif self.idb.wordsize == 8:
            return index & 0xFF00000000000000 == 0xFF00000000000000
        else:
            raise RuntimeError('unexpected wordsize')

    def _is_number(self, index):
        '''
        does the index look like not (address or node)?
        '''
        return (not self._is_address(index)) and (not self._is_node(index))

    def __getattr__(self, key):
        '''
        for the given field name, fetch the value from the appropriate netnode.
        if the field matches multiple indices, then return a mapping from index to value.

        Example::

            assert root.version == 695

        Example::

            assert 0x401000 in entrypoints.ordinals

        Example::

            assert entrypoints.ordinals[0] == 'DllMain'

        Args:
          key (str): the name of the field to fetch.

        Returns:
          any: if a parser was provided, then the parsed data.
            otherwise, the bytes associatd with the field.
            if the field matches multiple indices, then the result is mapping from index to value.

        Raises:
          KeyError: if the field does not exist.
        '''
        if key not in self._fields_by_name:
            return super(Analysis, self).__getattr__(key)

        field = self._fields_by_name[key]
        if field.index in VARIABLE_INDEXES:

            if field.index == ADDRESSES:
                nfilter = self._is_address
            elif field.index == NUMBERS:
                nfilter = self._is_number
            elif field.index == NODES:
                nfilter = self._is_node
            elif field.index == ALL:
                nfilter = lambda x: True
            else:
                raise ValueError('unexpected index')

            # indexes are variable, so map them to the values
            ret = {}
            for sup in self.netnode.supentries(tag=field.tag):
                if not nfilter(sup.parsed_key.index):
                    continue

                if field.cast is None:
                    ret[sup.parsed_key.index] = bytes(sup.value)
                else:
                    ret[sup.parsed_key.index] = field.cast(bytes(sup.value))
            return ret
        else:
            # normal field with an explicit index
            v = self.netnode.supval(field.index, tag=field.tag)
            if field.cast is None:
                return bytes(v)
            else:
                return field.cast(bytes(v))

    def get_field_tag(self, name):
        '''
        get the tag associated with the given field name.

        Example::

            assert root.get_field_tag('version') == 'A'

        Args:
          key (str): the name of the field to fetch.

        Returns:
          str: a single character string tag.
        '''
        return self._fields_by_name[name].tag

    def get_field_index(self, name):
        '''
        get the index associated with the given field name.
        Example::

            assert root.get_field_index('version') == -1

        Args:
          key (str): the name of the field to fetch.

        Returns:
          int or IndexType: the index, if its specified.
            otherwise, this will be an `IndexType` that indicates what indices are expected.
        '''
        return self._fields_by_name[name].index


def Analysis(nodeid, fields):
    '''
    build a partial constructor for _Analysis with the given nodeid and fields.

    Example::

        Root = Analysis('Root Node', [Field(...), ...])
        root = Root(some_idb)
        assert root.version == 695
    '''
    def inner(db):
        return _Analysis(db, nodeid, fields)
    return inner


ROOT_NODEID = 'Root Node'
class ROOT_INDEX:
    '''
    via: https://github.com/williballenthin/pyidbutil/blob/master/idbtool.py#L182
    '''
    VERSION = -1           # altval
    VERSION_STRING = 1303  # supval
    PARAM = 0x41b994       # supval
    OPEN_COUNT = -4        # altval
    CREATED = -2           # altval
    CRC = -5               # altval
    MD5 = 1302             # supval


Root = Analysis('Root Node', [
    Field('crc',            'A', -5,    idb.netnode.as_int),
    Field('open_count',     'A', -4,    idb.netnode.as_int),
    Field('created',        'A', -2,    as_unix_timestamp),
    Field('version',        'A', -1,    idb.netnode.as_int),
    Field('md5',            'S', 1302,  as_md5),
    Field('version_string', 'S', 1303,  idb.netnode.as_string),
    Field('param',          'S', 0x41b94, bytes),
])


LOADER_NODEID = '$ loader name'
class LOADER_INDEX:
    PLUGIN = 0x0  # supval
    FORMAT = 0x1  # supval


Loader = Analysis('$ loader name', [
    Field('plugin', 'S', 0, idb.netnode.as_string),
    Field('format', 'S', 1, idb.netnode.as_string),
])


User = Analysis('$ user1', [
    Field('data', 'S', 0, bytes),
])


# '$ entry points' maps from ordinal/address to function name.
#
# supvals:
#   format1
#     index: export ordinal
#     value: function name
#   format2
#     index: EA
#     value: function name
EntryPoints = Analysis('$ entry points', [
    Field('ordinals',  'S', NUMBERS, idb.netnode.as_string),
    Field('addresses', 'S', ADDRESSES, idb.netnode.as_string),
    Field('all',       'S', ALL, idb.netnode.as_string),
])


class FileRegion(vstruct.VStruct):
    def __init__(self):
        vstruct.VStruct.__init__(self)
        self.start = v_uint32()
        self.end = v_uint32()
        self.rva = v_uint32()


# '$ fileregions' maps from segment start address to details about it.
#
# supvals:
#   format1:
#     index: start effective address
#     value:
#       0x0: start effective address
#       0x4: end effective address
#       0x8: rva start?
FileRegions = Analysis('$ fileregions', [
    Field('regions',  'S', ADDRESSES, as_cast(FileRegion))
])


# nodeid: ff000022 tag: S index: 0x689bd410
# FF 68 9B D4 10 81 5A FF  44 10 99 CE 20 04 00 10 00 00 00 00 00 00
# [] [addr be  ] [] [   ]
#                flags, if 0x80 set, then next is 2 bytes
#
# nodeid: ff000022 tag: S index: 0x689bd56a
# FF 68 9B D5 6A 2D FF     80  00 C0 0A 48 05 01
# [] [addr be  ] [] []     []
#                flags, if 0x80 set, then next 2 bytes

class FunctionEntry(vstruct.VStruct):
    def __init__(self):
        vstruct.VStruct.__init__(self)
        self.unk0  = v_uint8()
        self.start = v_uint32(bigend=True)
        self.flags = v_uint8()
        self.unk05 = v_bytes()

    def pcb_flags(self):
        if self.flags & 0x80:
            self['unk05'].vsSetLength(1)
        else:
            self['unk05'].vsSetLength(0)


# '$ funcs' maps from function effective address to details about it.
#
# supvals:
#   format1:
#     index: effective address
#     value:
#       0x0:
#       0x1: start effective address (big endian)
#       0x4:
#       0x8:
#       0xC:
Functions = Analysis('$ funcs', [
    Field('functions',  'S', ADDRESSES, as_cast(FunctionEntry)),
])


class PString(vstruct.VStruct):
    '''
    short pascal string, prefixed with single byte length.
    '''
    def __init__(self):
        vstruct.VStruct.__init__(self)
        self.length = v_uint8()
        self.s = v_str()

    def pcb_length(self):
        length = self.length
        self['s'].vsSetLength(length - 1)


class TypeString(vstruct.VStruct):
    def __init__(self):
        vstruct.VStruct.__init__(self)
        self.header = v_uint8()
        self.length = v_uint8()
        self.s = v_str()

    def pcb_header(self):
        if self.header != 0x3D:
            raise RuntimeError('unexpected type header')

    def pcb_length(self):
        length = self.length
        self['s'].vsSetLength(length - 1)


class StructMember:
    def __init__(self, db, nodeid):
        self.idb = db
        self.nodeid = nodeid
        self.netnode = idb.netnode.Netnode(db, self.nodeid)

    def get_name(self):
        return self.netnode.name().partition('.')[2]

    def get_type(self):
        # nodeid: ff000078 tag: S index: 0x3000
        # 00000000: 3D 0A 48 49 4E 53 54 41  4E 43 45 00              =.HINSTANCE.

        v = self.netnode.supval(tag='S', index=0x3000)
        s = TypeString()
        s.vsParse(v)
        return s.s

    def get_enum_id(self):
        return self.altval(tag='A', index=0xB)

    def get_struct_id(self):
        return self.altval(tag='A', index=0x3)

    def get_member_comment(self):
        return self.supstr(tag='S', index=0x0)

    def get_repeatable_member_comment(self):
        return self.supstr(tag='S', index=0x1)

    # TODO: tag='A', index=0x10
    # TODO: tag='S', index=0x9, "ptrseg"

    def __str__(self):
        try:
            typ = self.get_type()
        except KeyError:
            return 'StructMember(name: %s)' % (self.get_name())
        else:
            return 'StructMember(name: %s, type: %s)' % (self.get_name(), self.get_type())


class STRUCT_FLAGS:
    # via: https://www.hex-rays.com/products/ida/support/sdkdoc/group___s_f__.html

    # is variable size structure (varstruct)? More...
    SF_VAR = 0x00000001

    # is a union? More...
    SF_UNION = 0x00000002

    # has members of type "union"?
    SF_HASUNI = 0x00000004

    # don't include in the chooser list
    SF_NOLIST = 0x00000008

    # the structure comes from type library
    SF_TYPLIB = 0x00000010

    # the structure is collapsed
    SF_HIDDEN = 0x00000020

    # the structure is a function frame
    SF_FRAME = 0x00000040

    # alignment (shift amount: 0..31)
    SF_ALIGN = 0x00000F80

    # ghost copy of a local type
    SF_GHOST = 0x00001000



class Struct:
    '''
    Example::

        struc = Struct(idb, 0xFF000075)
        assert struc.get_name() == 'EXCEPTION_INFO'
        assert len(struc.get_members()) == 5
        assert list(struc.get_members())[0].get_type() == 'DWORD'
    '''
    def __init__(self, db, structid):
        self.idb = db
        self.nodeid = structid
        self.netnode = idb.netnode.Netnode(db, self.nodeid)

    def get_members(self):
        v = self.netnode.supval(tag='M', index=0)
        vals = list(unpack_dds(v))

        if not vals[0] & STRUCT_FLAGS.SF_FRAME:
            raise RuntimeError('unexpected frame header')

        count = vals[1]
        offset = 2
        for i in range(count):
            if self.idb.wordsize == 4:
                member_vals = vals[offset:offset + 5]
                offset += 5
                nodeid_offset, unk1, unk2, unk3, unk4 = member_vals
                member_nodeid = self.netnode.nodebase + nodeid_offset
                yield StructMember(self.idb, member_nodeid)
            elif self.idb.wordsize == 8:
                member_vals = vals[offset:offset + 8]
                offset += 8
                nodeid_offseta, nodeid_offsetb, unk1a, unk1b, unk2a, unk2b, unk3, unk4 = member_vals
                nodeid_offset = nodeid_offseta | (nodeid_offset << 32)
                unk1 = unk1a | (unk1b << 32)
                unk2 = unk2a | (unk2b << 32)
                member_nodeid = self.netnode.nodebase + nodeid_offset
                yield StructMember(self.idb, member_nodeid)
            else:
                raise RuntimeError('unexpected wordsize')


def chunks(l, n):
    '''
    Yield successive n-sized chunks from l.
    via: https://stackoverflow.com/a/312464/87207
    '''
    if isinstance(l, types.GeneratorType):
        while True:
            v = list(itertools.islice(l, n))
            if not v:
                return
            yield v
    else:
        i = 0
        while True:
            try:
                v = l[i:i+n]
                yield v
            except IndexError:
                return
            i += n


def pairs(l):
    return chunks(l, 2)


Chunk = namedtuple('Chunk', ['effective_address', 'length'])
FunctionParameter = namedtuple('FunctionParameter', ['type', 'name'])
FunctionSignature = namedtuple('FunctionSignature', ['calling_convention', 'rtype', 'unk', 'parameters'])
StackChangePoint = namedtuple('StackChangePoint', ['effective_address', 'change'])


class Function:
    '''
    Example::

        func = Function(idb, 0x401000)
        assert func.get_name() == 'DllEntryPoint'
        assert func.get_signature() == '... DllEntryPoint(...)'
    '''
    def __init__(self, db, fva):
        self.idb = db
        self.nodeid = fva
        self.netnode = idb.netnode.Netnode(db, self.nodeid)

    def get_name(self):
        try:
            return self.netnode.name()
        except KeyError:
            return 'sub_%X' % (self.nodeid)

    def get_signature(self):
        typebuf = self.netnode.supval(tag='S', index=0x3000)
        namebuf = self.netnode.supval(tag='S', index=0x3001)

        if typebuf[0] != 0xC:
            raise RuntimeError('unexpected signature header')

        if typebuf[1] == ord('S'):
            # this is just a guess...
            conv = 'stdcall'
        else:
            raise NotImplementedError()

        rtype = TypeString()
        rtype.vsParse(typebuf, offset=2)

        # this is a guess???
        sp_delta = typebuf[2+len(rtype)]

        params = []
        typeoffset = 0x2 + len(rtype) + 0x1
        nameoffset = 0x0
        while typeoffset < len(typebuf):
            if typebuf[typeoffset] == 0x0:
                break
            typename = TypeString()
            typename.vsParse(typebuf, offset=typeoffset)
            typeoffset += len(typename)

            paramname = PString()
            paramname.vsParse(namebuf, offset=nameoffset)
            nameoffset += len(paramname)

            params.append(FunctionParameter(typename.s, paramname.s))

        return FunctionSignature(conv, rtype.s, sp_delta, params)

    def get_chunks(self):
        v = self.netnode.supval(tag='S', index=0x7000)

        # stored as:
        #
        #   first chunk:
        #     effective addr
        #     length
        #   second chunk:
        #     delta from first.ea + first.length
        #     length
        #   third chunk:
        #     delta from second.ea + second.length
        #     length
        #   ...

        last_ea = 0
        last_length = 0
        for delta, length in pairs(unpack_dds(v)):
            ea = last_ea + last_length + delta
            yield Chunk(ea, length)
            last_ea = ea
            last_length = length

    # S-0x1000: sp change points
    # S-0x4000: register variables
    # S-0x5000: local labels
    # S-0x7000: function tails

    def get_stack_change_points(self):
        # ref: ida.wll@0x100793d0
        v = self.netnode.supval(tag='S', index=0x1000)
        offset = self.nodeid
        for (delta, change) in pairs(unpack_dds(v)):
            offset += delta
            if change & 1:
                change = change >> 1
            else:
                change = -(change >> 1)

            yield StackChangePoint(offset, change)
