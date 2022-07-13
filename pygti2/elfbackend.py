import logging
import re

from elftools.dwarf.dwarfinfo import DWARFInfo
from elftools.dwarf.dwarf_expr import DW_OP_opcode2name
from elftools.dwarf.descriptions import _DESCR_DW_ATE
from elftools.dwarf.die import DIE
from elftools.elf.elffile import ELFFile

logger = logging.getLogger(__name__)


def loc2addr(die: DIE):
    if "DW_AT_location" not in die.attributes:
        return None
    addr = die.attributes["DW_AT_location"].value
    if DW_OP_opcode2name[addr[0]] != "DW_OP_addr":
        raise ValueError("Not an address.")
    addr = addr[1:]
    if len(addr) != die.dwarfinfo.config.default_address_size:
        raise NotImplementedError("No idea how to parse this address.")
    return int.from_bytes(addr, "little" if die.dwarfinfo.config.little_endian else "big")


def dw_at_encoding(die: DIE):
    map = dict(enumerate(_DESCR_DW_ATE.values()))
    return map[die.attributes["DW_AT_encoding"].value]


class CType:
    def __init__(self, backend: "ElfBackend", typename: str, size: int):
        self.backend = backend
        self.kind = "none"
        self.typename = typename
        self.size = size

    def fromdie(cls, backend: "ElfBackend", die: DIE, *args, **kwargs) -> "CType":
        if die and "DW_AT_name" in die.attributes:
            typename = die.attributes["DW_AT_name"].value.decode()
        else:
            typename = "?"

        if die and "DW_AT_byte_size" in die.attributes:
            size = die.attributes["DW_AT_byte_size"].value
        else:
            size = -1

        return cls(backend=backend, typename=typename, size=size, *args, **kwargs)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CType):
            if other.typename == self.typename:
                return True
        return False

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} "{self.typename}">'


class CTypeBaseType(CType):
    """
    Types with tag "DW_TAG_base_type"
    """

    @classmethod
    def fromdie(cls, backend: "ElfBackend", die: DIE) -> "CTypeBaseType":
        encoding = dw_at_encoding(die)
        if encoding in ("(unsigned)", "(unsigned char)", "(boolean)"):
            return CType.fromdie(CTypeBaseInt, backend, die)
        elif encoding in ("(signed)", "(signed char)"):
            return CType.fromdie(CTypeBaseInt, backend, die, signed=True)
        elif encoding in ("(float)",):
            return CType.fromdie(CTypeBaseFloat, backend, die)
        else:
            raise NotImplementedError(f"Unkown encoding: {encoding}")


class CTypeBaseInt(CTypeBaseType):
    """
    Types with tag "DW_TAG_base_type" and integer like encoding
    """

    def __init__(self, backend, typename, size, signed: bool = False):
        super().__init__(backend, typename, size)
        self.kind = "int"
        self.signed = signed
        logger.debug("New " + repr(self))


class CTypeBaseFloat(CTypeBaseType):
    """
    Types with tag "DW_TAG_base_type" and float like encoding
    """

    def __init__(self, backend, typename, size):
        super().__init__(backend, typename, size)
        self.kind = "float"
        logger.debug("New " + repr(self))


class CTypePointer(CType):
    def __init__(self, backend, typename, size, base: CType):
        if base is not None and base.typename != "?":
            typename = f"{base.typename} *"
        super().__init__(backend, typename, size)
        self.kind = "pointer"
        self.base = base
        logger.debug("New " + repr(self))

    @classmethod
    def fromdie(cls, backend: "ElfBackend", die: DIE) -> "CTypePointer":
        if "DW_AT_type" not in die.attributes:
            return CTypePointer(backend, "void *", 0, backend.type_from_string("void"))
        basedie = die.get_DIE_from_attribute("DW_AT_type")
        base = backend.type_from_die(basedie)
        if base is None and "DW_AT_type" in basedie.attributes:
            base = backend.type_from_die(basedie.get_DIE_from_attribute("DW_AT_type"))
        if base is None:
            base = backend.types["void"]
        return CType.fromdie(CTypePointer, backend, die, base=base)


class CTypeArray(CType):
    def __init__(self, backend, base: CType, length: int, *args, **kwargs):
        self.length = length
        if self.length == -1 or base.size == -1:
            size = -1
        else:
            size = self.length * base.size
        typename = f"{base.typename} [{self.length}]"
        super().__init__(backend, typename, size)
        self.base = base
        self.kind = "array"
        logger.debug("New " + repr(self))

    @classmethod
    def fromdie(cls, backend: "ElfBackend", die: DIE) -> "CTypeArray":
        base = backend.type_from_die(die.get_DIE_from_attribute("DW_AT_type"))

        length = -1
        for child in die.iter_children():
            if child.tag != "DW_TAG_subrange_type":
                continue
            if "DW_AT_upper_bound" in child.attributes:
                length = child.attributes["DW_AT_upper_bound"].value + 1
                break

        return CType.fromdie(CTypeArray, backend, die, base=base, length=length)

    def update(self, other: "CTypeArray"):
        if self.length == -1 and other.length != -1:
            self.length = other.length
            self.size = other.size

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} "{self.typename}">'


class CTypeTypedef(CType):
    """
    Types with tag "DW_TAG_typedef".
    """

    def __init__(self, backend, typename, size, base: CTypeBaseType):
        CType.__init__(self, backend, typename, base.size)
        self.base = base

    @classmethod
    def fromdie(cls, backend: "ElfBackend", die: DIE) -> "CTypeTypedef":
        if "DW_AT_type" not in die.attributes:
            return None
        base = backend.type_from_die(die.get_DIE_from_attribute("DW_AT_type"))
        if base.kind == "int":
            return CType.fromdie(CTypeTypedefInt, backend, die, base=base)
        elif base.kind == "struct":
            return CType.fromdie(CTypeTypedefStruct, backend, die, base=base)
        elif base.kind == "pointer":
            return CType.fromdie(CTypeTypedefPointer, backend, die, base=base)
        elif base.kind == "array":
            return CType.fromdie(CTypeTypedefArray, backend, die, base=base)
        elif base.kind == "union":
            return CType.fromdie(CTypeTypedefUnion, backend, die, base=base)
        else:
            raise NotImplementedError(f"Unkown base kind: {base}")


class CTypeTypedefInt(CTypeTypedef):
    """
    Types with tag "DW_AT_typedef" and int like.
    """

    def __init__(self, backend, typename, size, base: CTypeBaseType):
        super().__init__(backend, typename, size, base)
        self.kind = "int"
        if hasattr(base, "signed"):
            self.signed = base.signed
        logger.debug("New " + repr(self))


class CTypeStruct(CType):
    """
    Types with tag "DW_TAG_structure_type".
    """

    @classmethod
    def fromdie(cls, backend: "ElfBackend", die: DIE) -> "CTypeStruct":
        return CType.fromdie(CTypeStruct, backend, die)

    def __init__(self, backend, typename, size):
        super().__init__(backend, typename, size)
        self.kind = "struct"
        if self.typename != "?":
            self.typename = f"struct {self.typename}"
        logger.debug("New " + repr(self))

    def _create_members(self, die):
        members = {}
        for child in die.iter_children():
            child: DIE = child
            if child.tag != "DW_TAG_member":
                continue
            membertypedie = child.get_DIE_from_attribute("DW_AT_type")
            membertype = self.backend.type_from_die(membertypedie)
            # if membertypedie != die:
            #     membertype = self.backend.type_from_die(membertypedie)
            # else:
            #     membertype = self
            members[child.attributes["DW_AT_name"].value.decode()] = (
                child.attributes["DW_AT_data_member_location"].value,
                membertype,
            )
        self.members = members
        logger.debug(f"Create members for {repr(self)}: {self.members}")


class CTypeTypedefStruct(CTypeTypedef, CTypeStruct):
    """
    Types with tag "DW_TAG_typedef" and type CTypeStructure.
    """

    def __init__(self, backend, typename, size, base: CTypeStruct):
        CTypeTypedef.__init__(self, backend, typename, size, base)
        self.kind = "struct"
        logger.debug("New " + repr(self))

    def _create_members(self, die):
        return CTypeStruct._create_members(self, die.get_DIE_from_attribute("DW_AT_type"))


class CTypeUnion(CType):
    """
    Types with tag "DW_TAG_union_type".
    """

    @classmethod
    def fromdie(cls, backend: "ElfBackend", die: DIE) -> "CTypeUnion":
        return CType.fromdie(CTypeUnion, backend, die)

    def __init__(self, backend, typename, size):
        super().__init__(backend, typename, size)
        self.kind = "union"
        if self.typename != "?":
            self.typename = f"union {self.typename}"
        logger.debug("New " + repr(self))

    def _create_members(self, die):
        members = {}
        for child in die.iter_children():
            child: DIE = child
            if child.tag != "DW_TAG_member":
                continue
            membertypedie = child.get_DIE_from_attribute("DW_AT_type")
            membertype = self.backend.type_from_die(membertypedie)
            members[child.attributes["DW_AT_name"].value.decode()] = (0, membertype)
        self.members = members


class CTypeTypedefUnion(CTypeTypedef, CTypeUnion):
    """
    Types with tag "DW_TAG_typedef" and type CTypeUnionure.
    """

    def __init__(self, backend, typename, size, base: CTypeUnion):
        CTypeTypedef.__init__(self, backend, typename, size, base)
        self.kind = "union"

    def _create_members(self, die):
        return CTypeUnion._create_members(self, die.get_DIE_from_attribute("DW_AT_type"))


class CTypeTypedefPointer(CTypeTypedef, CTypePointer):
    """
    Types with tag "DW_TAG_typedef" and type CTypePointer.
    """

    def __init__(self, backend, typename, size, base: CTypePointer):
        CTypeTypedef.__init__(self, backend, typename, size, base)
        self.kind = "pointer"
        logger.debug("New " + repr(self))


class CTypeTypedefArray(CTypeTypedef, CTypeArray):
    """
    Types with tag "DW_TAG_typedef_type" and type CTypeArray.
    """

    def __init__(self, backend, typename, size, base: CTypeArray):
        CTypeTypedef.__init__(self, backend, typename, base.size, base)
        self.kind = "array"
        logger.debug("New " + repr(self))

    def update(self, other: "CTypeTypedefArray"):
        if self.size == -1:
            self.size = other.size
            self.base.update(other.base)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.base}>"


class CTypeEnumeration(CType):
    """
    Types with tag "DW_TAG_enumeration_type".
    """

    @classmethod
    def fromdie(cls, backend: "ElfBackend", die: DIE) -> "CTypeBaseType":
        type = CType.fromdie(CTypeEnumeration, backend, die)
        type._create_enums(die)
        return type

    def __init__(self, backend, typename, size):
        super().__init__(backend, typename, size)
        self.kind = "int"
        logger.debug("New " + repr(self))

    def _create_enums(self, die):
        for child in die.iter_children():
            child: DIE = child
            if child.tag != "DW_TAG_enumerator":
                continue
            self.backend.enums[child.attributes["DW_AT_name"].value.decode()] = child.attributes[
                "DW_AT_const_value"
            ].value


class CTypeVariable(CType):
    """
    Types with tag "DW_TAG_variable_type".
    """

    @classmethod
    def fromdie(cls, backend: "ElfBackend", die: DIE) -> "CTypeVariable":
        location = None
        if "DW_AT_specification" in die.attributes:
            location = loc2addr(die)
            die = die.get_DIE_from_attribute("DW_AT_specification")
        elif "DW_AT_location" in die.attributes:
            location = loc2addr(die)
        typedie = die.get_DIE_from_attribute("DW_AT_type")
        if typedie.tag == "DW_TAG_const_type":
            typedie = typedie.get_DIE_from_attribute("DW_AT_type")
        type = backend.type_from_die(typedie)
        return CType.fromdie(CTypeVariable, backend, die, type=type, location=location)

    def __init__(self, backend, typename, size, type: CType, location: int = None):
        super().__init__(backend, typename, size)
        self.kind = "variable"
        self.type = type
        self.address = location
        self.size = type.size
        logger.debug("New " + repr(self))

    def update(self, other: "CTypeVariable"):
        if self.address is None:
            self.address = other.address
            self.type = other.type
        if self.size == -1:
            self.size = other.size
            self.type = other.type


class CTypeFunction(CType):
    """
    Types with tag "DW_TAG_function".
    """

    @classmethod
    def fromdie(cls, backend: "ElfBackend", die: DIE) -> "CTypeFunction":
        if "DW_AT_low_pc" not in die.attributes:
            # Functions without location are useless
            return None
        if "DW_AT_abstract_origin" in die.attributes:
            basedie = die.get_DIE_from_attribute("DW_AT_abstract_origin")
        else:
            basedie = die

        if "DW_AT_low_pc" in die.attributes:
            address = die.attributes["DW_AT_low_pc"].value
        elif basedie is not None:
            address = basedie.attributes["DW_AT_low_pc"].value

        type = CType.fromdie(CTypeFunction, backend, die, address=address)
        type._create_argument_types(basedie)
        return type

    def __init__(self, backend, typename, size, address):
        super().__init__(backend, typename, size)
        self.kind = "function"
        self.address = address
        logger.debug("New " + repr(self))

    def _create_argument_types(self, die: DIE):
        if "DW_AT_type" in die.attributes:
            self.return_type = self.backend.type_from_die(die.get_DIE_from_attribute("DW_AT_type"))
        else:
            self.return_type = None
        self.arguments = []
        for child in die.iter_children():
            if child.tag != "DW_TAG_formal_parameter":
                continue
            if "DW_AT_type" in child.attributes:
                argdie = child.get_DIE_from_attribute("DW_AT_type")
            elif "DW_AT_abstract_origin" in child.attributes:
                argdie = child.get_DIE_from_attribute("DW_AT_abstract_origin")
            self.arguments.append(self.backend.type_from_die(argdie))


class ElfBackend:
    def __init__(
        self,
        file: str,
        readelf_binary="readelf",
        compilation_unit_filter=lambda _: True,
        tolerant: bool = True,
    ):
        self.types = {}
        self.enums = {}
        self.types["void"] = CTypeBaseType(self, "void", 0)
        self._create(file, readelf_binary, compilation_unit_filter, tolerant)
        self.types["NULL"] = CTypeVariable(self, "NULL", 0, self.type_from_string("void *"), 0)

    def type_from_die(self, die: DIE):
        if die.tag == "DW_TAG_base_type":
            type = CTypeBaseType.fromdie(self, die)
        elif die.tag == "DW_TAG_typedef":
            type = CTypeTypedef.fromdie(self, die)
        elif die.tag == "DW_TAG_structure_type":
            type = CTypeStruct.fromdie(self, die)
        elif die.tag == "DW_TAG_union_type":
            type = CTypeUnion.fromdie(self, die)
        elif die.tag == "DW_TAG_pointer_type":
            type = CTypePointer.fromdie(self, die)
        elif die.tag == "DW_TAG_array_type":
            type = CTypeArray.fromdie(self, die)
        elif die.tag == "DW_TAG_variable":
            type = CTypeVariable.fromdie(self, die)
        elif die.tag == "DW_TAG_subprogram":
            type = CTypeFunction.fromdie(self, die)
        elif die.tag == "DW_TAG_enumeration_type":
            type = CTypeEnumeration.fromdie(self, die)
        elif die.tag == "DW_TAG_const_type":
            if "DW_AT_type" in die.attributes:
                type = self.type_from_die(die.get_DIE_from_attribute("DW_AT_type"))
            else:
                type = self.types["void"]
        else:
            return None

        if type is None:
            return None
        if type.typename == "?":
            return type
        if type.typename in self.types:
            other = self.types[type.typename]
            if hasattr(other, "update"):
                other.update(type)
            return other
        else:
            self.types[type.typename] = type
            if type.kind in ("struct", "union") and not hasattr(type, "members"):
                type._create_members(die)
            return type

    def _create(
        self,
        file: str,
        readelf_binary="readelf",
        compilation_unit_filter=lambda _: True,
        tolerant=True,
    ):
        with open(file, "rb") as fp:
            self.elffile: ELFFile = ELFFile(fp)
            self.dwarfinfo: DWARFInfo = self.elffile.get_dwarf_info()
            self.endian: str = "little" if self.dwarfinfo.config.little_endian else "big"
            self.sizeof_voidp: int = self.dwarfinfo.config.default_address_size
            for cu in self.dwarfinfo.iter_CUs():
                cuname = cu.get_top_DIE().attributes["DW_AT_name"].value.decode()
                if not compilation_unit_filter(cuname):
                    logger.debug(f'ElfBackend: Skip "{cuname}"')
                    continue
                depth = 0
                for die in cu.iter_DIEs():
                    if die.is_null():
                        depth -= 1
                        continue
                    if depth == 1:
                        if "DW_AT_name" in die.attributes:
                            logger.debug(
                                f"ElfBackend: Create from {die.tag} @ {die.offset} "
                                f'{die.attributes["DW_AT_name"].value.decode()}"'
                            )
                        else:
                            logger.debug(f"ElfBackend: Create from {die.tag} @ {die.offset}")
                        self.type_from_die(die)
                    if die.has_children:
                        depth += 1

    def type_from_string(self, decl: str):
        if decl in self.types:
            return self.types[decl]
        match = re.match(
            r"^(?P<base_pointer>[\w ]+\w(?: ?\*)*) ?\*$|"
            r"^(?P<base_array>[\w ]+\w) ?\[(?P<array_length>\d+)?\]$",
            decl,
        )
        if not match:
            raise TypeError(f'Cannot create type from "{decl}".')

        if match.group("base_pointer"):
            base = self.types[match.group("base_pointer")]
            type = CTypePointer(self, f"{base.typename} *", self.sizeof_voidp, base)
        elif match.group("base_array"):
            base = self.types[match.group("base_array")]
            type = CTypeArray(
                backend=self,
                base=base,
                length=int(match.group("array_length"), 0) if match.group("array_length") else -1,
            )
        else:
            raise TypeError(f'Cannot create type from "{decl}".')

        if type.typename == "?":
            return type
        if type.typename in self.types:
            return self.types[type.typename]
        else:
            self.types[type.typename] = type
            return type
