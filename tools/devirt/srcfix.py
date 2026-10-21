"""Source-level fixes for unluac output (text handled as latin-1 so bytes round-trip)."""
import re

# an identifier-like run that contains a byte >= 0x80 (unluac treats those as letters)
_HIGH_NAME = r'[A-Za-z0-9_\x80-\xff]*[\x80-\xff][A-Za-z0-9_\x80-\xff]*'


def _quote(name):
    return '"' + ''.join('\\%d' % ord(ch) if ord(ch) >= 0x80 else ch for ch in name) + '"'


def high_byte_names(text):
    """t.<name> -> t["<name>"], t:<name>( -> t["<name>"](t, ...) is not needed (never a method
    name here), and constructor keys <name> = v -> ["<name>"] = v."""
    text = re.sub(r'\.(' + _HIGH_NAME + r')', lambda m: '[' + _quote(m.group(1)) + ']', text)
    text = re.sub(r'([{,]\s*)(' + _HIGH_NAME + r')(\s*=[^=])',
                  lambda m: m.group(1) + '[' + _quote(m.group(2)) + ']' + m.group(3), text)
    return text
