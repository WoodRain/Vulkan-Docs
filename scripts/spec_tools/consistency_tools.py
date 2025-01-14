#!/usr/bin/python3 -i
#
# Copyright (c) 2019 Collabora, Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author(s):    Ryan Pavlik <ryan.pavlik@collabora.com>
"""Provides utilities to write a script to verify XML registry consistency."""

import re

from .algo import RecursiveMemoize
from .attributes import ExternSyncEntry, LengthEntry
from .util import findNamedElem, getElemName
from .data_structures import DictOfStringSets


class XMLChecker:
    def __init__(self, entity_db,  conventions, manual_types_to_codes=None,
                 forward_only_types_to_codes=None,
                 reverse_only_types_to_codes=None,
                 suppressions=None):
        """Set up data structures.

        May extend - call:
        `super().__init__(db, conventions, manual_types_to_codes)`
        as the last statement in your function.

        manual_types_to_codes is a dictionary of hard-coded
        "manual" return codes:
        the codes of the value are available for a command if-and-only-if
        the key type is passed as an input.

        forward_only_types_to_codes is additional entries to the above
        that should only be used in the "forward" direction
        (arg type implies return code)

        reverse_only_types_to_codes is additional entries to
        manual_types_to_codes that should only be used in the
        "reverse" direction
        (return code implies arg type)
        """
        self.fail = False
        self.entity = None
        self.errors = DictOfStringSets()
        self.warnings = DictOfStringSets()
        self.db = entity_db
        self.reg = entity_db.registry
        self.handle_data = HandleData(self.reg)
        self.conventions = conventions

        self.CONST_RE = re.compile(r"\bconst\b")
        self.ARRAY_RE = re.compile(r"\[[^]]+\]")

        # Init memoized properties
        self._handle_data = None

        if not manual_types_to_codes:
            manual_types_to_codes = {}
        if not reverse_only_types_to_codes:
            reverse_only_types_to_codes = {}
        if not forward_only_types_to_codes:
            forward_only_types_to_codes = {}

        reverse_codes = DictOfStringSets(reverse_only_types_to_codes)
        forward_codes = DictOfStringSets(forward_only_types_to_codes)
        for k, v in manual_types_to_codes.items():
            forward_codes.add(k, v)
            reverse_codes.add(k, v)

        self.forward_only_manual_types_to_codes = forward_codes.get_dict()
        self.reverse_only_manual_types_to_codes = reverse_codes.get_dict()

        # The presence of some types as input to a function imply the
        # availability of some return codes.
        self.input_type_to_codes = compute_type_to_codes(
            self.handle_data,
            forward_codes,
            extra_op=self.add_extra_codes)

        # Some return codes require a type (or its child) in the input.
        self.codes_requiring_input_type = compute_codes_requiring_type(
            self.handle_data,
            reverse_codes
        )

        self.referenced_input_types = ReferencedTypes(self.db, self.is_input)
        self.referenced_api_types = ReferencedTypes(self.db, self.is_api_type)
        if not suppressions:
            suppressions = {}
        self.suppressions = DictOfStringSets(suppressions)

    def is_api_type(self, member_elem):
        """Return true if the member/parameter ElementTree passed is from this API.

        May override or extend."""
        membertext = "".join(member_elem.itertext())

        return self.conventions.type_prefix in membertext

    def is_input(self, member_elem):
        """Return true if the member/parameter ElementTree passed is
        considered "input".

        May override or extend."""
        membertext = "".join(member_elem.itertext())

        if self.conventions.type_prefix not in membertext:
            return False

        ret = True
        # Const is always input.
        if self.CONST_RE.search(membertext):
            ret = True

        # Arrays and pointers that aren't const are always output.
        elif "*" in membertext:
            ret = False
        elif self.ARRAY_RE.search(membertext):
            ret = False

        return ret

    def add_extra_codes(self, types_to_codes):
        """Add any desired entries to the types-to-codes DictOfStringSets
        before performing "ancestor propagation".

        Passed to compute_type_to_codes as the extra_op.

        May override."""
        pass

    def should_skip_checking_codes(self, name):
        """Return True if more than the basic validation of return codes should
        be skipped for a command.

        May override."""
        return False

    def get_codes_for_command_and_type(self, cmd_name, type_name):
        """Return a set of error codes expected due to having
        an input argument of type type_name.

        The cmd_name is passed for use by extending methods.

        May extend."""
        return self.input_type_to_codes.get(type_name, set())

    def check(self):
        """Iterate through the registry, looking for consistency problems.

        Outputs error messages at the end."""
        # Iterate through commands, looking for consistency problems.
        for name, info in self.reg.cmddict.items():
            self.set_error_context(entity=name, elem=info.elem)

            self.check_command(name, info)

        for name, info in self.reg.typedict.items():
            cat = info.elem.get('category')
            if not cat:
                # This is an external thing, skip it.
                continue
            self.set_error_context(entity=name, elem=info.elem)

            self.check_type(name, info, cat)

        for name, info in self.reg.extdict.items():
            if info.elem.get('supported') != self.conventions.xml_api_name:
                # Skip unsupported extensions
                continue
            self.set_error_context(entity=name, elem=info.elem)
            self.check_extension(name, info)

        entities_with_messages = set(
            self.errors.keys()).union(self.warnings.keys())

        for entity in entities_with_messages:
            print()
            print('-------------------')
            print('Messages for', entity)
            print()
            messages = self.errors.get(entity)
            if messages:
                for m in messages:
                    print('Error:', m)

            messages = self.warnings.get(entity)
            if messages:
                for m in messages:
                    print('Warning:', m)

    def check_param(self, param):
        """Check a member of a struct or a param of a function.

        Called from check_params.

        May extend."""
        param_name = getElemName(param)
        externsyncs = ExternSyncEntry.parse_externsync_from_param(param)
        if externsyncs:
            for entry in externsyncs:
                if entry.entirely_extern_sync:
                    if len(externsyncs) > 1:
                        self.record_error("Comma-separated list in externsync attribute includes 'true' for",
                                          param_name)
                else:
                    # member name
                    # TODO only looking at the superficial feature here,
                    # not entry.param_ref_parts
                    if entry.member != param_name:
                        self.record_error("externsync attribute for", param_name,
                                          "refers to some other member/parameter:", entry.member)

    def check_params(self, params):
        """Check the members of a struct or params of a function.

        Called from check_type and check_command.

        May extend."""
        for param in params:
            self.check_param(param)

            # Check for parameters referenced by len= attribute
            lengths = LengthEntry.parse_len_from_param(param)
            if lengths:
                for entry in lengths:
                    if not entry.other_param_name:
                        continue
                    # TODO only looking at the superficial feature here,
                    # not entry.param_ref_parts
                    other_param = findNamedElem(params, entry.other_param_name)
                    if other_param is None:
                        self.record_error("References a non-existent parameter/member in the length of",
                                          getElemName(param), ":", entry.other_param_name)

    def check_type(self, name, info, category):
        """Check a type's XML data for consistency.

        Called from check.

        May extend."""
        if category == 'struct':
            if not name.startswith(self.conventions.type_prefix):
                self.record_error("Name does not start with",
                                  self.conventions.type_prefix)
            self.check_params(info.elem.findall('member'))

        elif category == "bitmask":
            if 'Flags' not in name:
                self.record_error("Name of bitmask doesn't include 'Flags'")

    def check_extension(self, name, info):
        """Check an extension's XML data for consistency.

        Called from check.

        May extend."""
        pass

    def check_command(self, name, info):
        """Check a command's XML data for consistency.

        Called from check.

        May extend."""
        elem = info.elem

        self.check_params(elem.findall('param'))

        # Some minimal return code checking
        errorcodes = elem.get("errorcodes")
        if errorcodes:
            errorcodes = errorcodes.split(",")
        else:
            errorcodes = []

        successcodes = elem.get("successcodes")
        if successcodes:
            successcodes = successcodes.split(",")
        else:
            successcodes = []

        if not successcodes and not errorcodes:
            # Early out if no return codes.
            return

        # Create a set for each group of codes, and check that
        # they aren't duplicated within or between groups.
        errorcodes_set = set(errorcodes)
        if len(errorcodes) != len(errorcodes_set):
            self.record_error("Contains a duplicate in errorcodes")

        successcodes_set = set(successcodes)
        if len(successcodes) != len(successcodes_set):
            self.record_error("Contains a duplicate in successcodes")

        if not successcodes_set.isdisjoint(errorcodes_set):
            self.record_error("Has errorcodes and successcodes that overlap")

        self.check_command_return_codes_basic(
            name, info, successcodes_set, errorcodes_set)

        # Continue to further return code checking if not "complicated"
        if not self.should_skip_checking_codes(name):
            codes_set = successcodes_set.union(errorcodes_set)
            self.check_command_return_codes(
                name, info, successcodes_set, errorcodes_set, codes_set)

    def check_command_return_codes_basic(self, name, info,
                                         successcodes, errorcodes):
        """Check a command's return codes for consistency.

        Called from check_command on every command.

        May extend."""

        # Check that all error codes include _ERROR_,
        #  and that no success codes do.
        for code in errorcodes:
            if "_ERROR_" not in code:
                self.record_error(
                    code, "in errorcodes but doesn't contain _ERROR_")

        for code in successcodes:
            if "_ERROR_" in code:
                self.record_error(code, "in successcodes but contain _ERROR_")

    def check_command_return_codes(self, name, type_info,
                                   successcodes, errorcodes,
                                   codes):
        """Check a command's return codes in-depth for consistency.

        Called from check_command, only if
        `self.should_skip_checking_codes(name)` is False.

        May extend."""
        referenced_input = self.referenced_input_types[name]
        referenced_types = self.referenced_api_types[name]

        # Check that we have all the codes we expect, based on input types.
        for referenced_type in referenced_input:
            required_codes = self.get_codes_for_command_and_type(
                name, referenced_type)
            missing_codes = required_codes - codes
            if missing_codes:
                self.record_error("Missing expected return code(s)",
                                  ",".join(missing_codes),
                                  "implied because of input of type",
                                  referenced_type)

        # Check that, for each code returned by this command that we can
        # associate with a type, we have some type that can provide it.
        # e.g. can't have INSTANCE_LOST without an Instance
        # (or child of Instance).
        for code in codes:

            required_types = self.codes_requiring_input_type.get(code)
            if not required_types:
                # This code doesn't have a known requirement
                continue

            # TODO: do we look at referenced_types or referenced_input here?
            # the latter is stricter
            if not referenced_types.intersection(required_types):
                self.record_error("Unexpected return code", code,
                                  "- none of these types:",
                                  required_types,
                                  "found in the set of referenced types",
                                  referenced_types)

    ###
    # Utility properties/methods
    ###

    def set_error_context(self, entity=None, elem=None):
        """Set the entity and/or element for future record_error calls."""
        self.entity = entity
        self.elem = elem
        self.name = getElemName(elem)
        self.entity_suppressions = self.suppressions.get(getElemName(elem))

    def record_error(self, *args, **kwargs):
        """Record failure and an error message for the current context."""
        message = " ".join((str(x) for x in args))

        if self.entity_suppressions and message in self.entity_suppressions:
            return

        message = self._prepend_sourceline_to_message(message, **kwargs)
        self.fail = True
        self.errors.add(self.entity, message)

    def record_warning(self, *args, **kwargs):
        """Record a warning message for the current context."""
        message = " ".join((str(x) for x in args))

        if self.entity_suppressions and message in self.entity_suppressions:
            return

        message = self._prepend_sourceline_to_message(message, **kwargs)
        self.warnings.add(self.entity, message)

    def _prepend_sourceline_to_message(self, message, **kwargs):
        """Prepend a file and/or line reference to the message, if possible.

        If filename is given as a keyword argument, it is used on its own.

        If filename is not given, this will attempt to retrieve the filename and line from an XML element.
        If 'elem' is given as a keyword argument and is not None, it is used to find the line.
        If 'elem' is given as None, no XML elements are looked at.
        If 'elem' is not supplied, the error context element is used.

        If using XML, the filename, if available, is retrieved from the Registry class.
        If using XML and python-lxml is installed, the source line is retrieved from whatever element is chosen."""
        fn = kwargs.get('filename')
        sourceline = None

        if fn is None:
            elem = kwargs.get('elem', self.elem)
            if elem is not None:
                sourceline = getattr(elem, 'sourceline', None)
                if self.reg.filename:
                    fn = self.reg.filename

        if fn is None and sourceline is None:
            return message

        if fn is None:
            return "Line {}: {}".format(sourceline, message)

        if sourceline is None:
            return "{}: {}".format(fn, message)

        return "{}:{}: {}".format(fn, sourceline, message)


class HandleParents(RecursiveMemoize):
    def __init__(self, handle_types):
        self.handle_types = handle_types

        super().__init__(handle_types.keys())

    def compute(self, handle_type):
        immediate_parent = self.handle_types[handle_type].elem.get('parent')

        if immediate_parent is None:
            # No parents, no need to recurse
            return []

        # Support multiple (alternate) parents
        immediate_parents = immediate_parent.split(',')

        # Recurse, combine, and return
        all_parents = immediate_parents[:]
        for parent in immediate_parents:
            all_parents.extend(self[parent])
        return all_parents


def _always_true(x):
    return True


class ReferencedTypes(RecursiveMemoize):
    """Find all types(optionally matching a predicate) that are referenced
    by a struct or function, recursively."""

    def __init__(self, db, predicate=None):
        """Initialize.

        Provide an EntityDB object and a predicate function."""
        self.db = db

        self.predicate = predicate
        if not self.predicate:
            # Default predicate is "anything goes"
            self.predicate = _always_true
        super().__init__(permit_cycles=True)

    def compute(self, type_name):
        members = self.db.getMemberElems(type_name)
        if not members:
            return set()
        types = ((member, member.find("type")) for member in members)
        types = set(type_elem.text for (member, type_elem) in types
                    if type_elem is not None and self.predicate(member))
        all_types = set()
        all_types.update(types)
        for t in types:
            referenced = self[t]
            if referenced is not None:
                # If not leading to a cycle
                all_types.update(referenced)
        return all_types


class HandleData:
    """Data about all the handle types available in an API specification."""

    def __init__(self, registry):
        self.reg = registry
        self._handle_types = None
        self._ancestors = None
        self._descendants = None

    @property
    def handle_types(self):
        """Return a dictionary of handle type names to type info."""
        if not self._handle_types:
            # First time requested - compute it.
            self._handle_types = {
                type_name: type_info
                for type_name, type_info in self.reg.typedict.items()
                if type_info.elem.get('category') == 'handle'
            }
        return self._handle_types

    @property
    def ancestors_dict(self):
        """Return a dictionary of handle type names to sets of ancestors."""
        if not self._ancestors:
            # First time requested - compute it.
            self._ancestors = HandleParents(self.handle_types).get_dict()
        return self._ancestors

    @property
    def descendants_dict(self):
        """Return a dictionary of handle type names to sets of descendants."""
        if not self._descendants:
            # First time requested - compute it.

            handle_parents = self.ancestors_dict

            def get_descendants(handle):
                return set(h for h in handle_parents.keys()
                           if handle in handle_parents[h])

            self._descendants = {
                h: get_descendants(h)
                for h in handle_parents.keys()
            }
        return self._descendants


def compute_type_to_codes(handle_data, types_to_codes, extra_op=None):
    """Compute a DictOfStringSets of input type to required return codes.

    - handle_data is a HandleData instance.
    - d is a dictionary of type names to strings or string collections of
      return codes.
    - extra_op, if any, is called after populating the output from the input
      dictionary, but before propagation of parent codes to child types.
      extra_op is called with the in-progress DictOfStringSets.

    Returns a DictOfStringSets of input type name to set of required return
    code names.
    """
    # Initialize with the supplied "manual" codes
    types_to_codes = DictOfStringSets(types_to_codes)

    # Dynamically generate more codes, if desired
    if extra_op:
        extra_op(types_to_codes)

    # Final post-processing

    # Any handle can result in its parent handle's codes too.

    handle_ancestors = handle_data.ancestors_dict

    extra_handle_codes = {}
    for handle_type, ancestors in handle_ancestors.items():
        codes = set()
        # The sets of return codes corresponding to each ancestor type.
        ancestors_codes = (types_to_codes.get(ancestor, set())
                           for ancestor in ancestors)
        codes.union(*ancestors_codes)
        # for parent_codes in ancestors_codes:
        #     codes.update(parent_codes)
        extra_handle_codes[handle_type] = codes

    for handle_type, extras in extra_handle_codes.items():
        types_to_codes.add(handle_type, extras)

    return types_to_codes


def compute_codes_requiring_type(handle_data, types_to_codes, registry=None):
    """Compute a DictOfStringSets of return codes to a set of input types able
    to provide the ability to generate that code.

    handle_data is a HandleData instance.
    d is a dictionary of input types to associated return codes(same format
    as for input to compute_type_to_codes, may use same dict).
    This will invert that relationship, and also permit any "child handles"
    to satisfy a requirement for a parent in producing a code.

    Returns a DictOfStringSets of return code name to the set of parameter
    types that would allow that return code.
    """
    # Use DictOfStringSets to normalize the input into a dict with values
    # that are sets of strings
    in_dict = DictOfStringSets(types_to_codes)

    handle_descendants = handle_data.descendants_dict

    out = DictOfStringSets()
    for in_type, code_set in in_dict.items():
        descendants = handle_descendants.get(in_type)
        for code in code_set:
            out.add(code, in_type)
            if descendants:
                out.add(code, descendants)

    return out
