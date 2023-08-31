#!/usr/bin/env python
#
# A library that provides a Python interface to the Telegram Bot API
# Copyright (C) 2015-2023
# Leandro Toledo de Souza <devs@python-telegram-bot.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser Public License for more details.
#
# You should have received a copy of the GNU Lesser Public License
# along with this program.  If not, see [http://www.gnu.org/licenses/].
import inspect
import os
import re
from datetime import datetime
from typing import Any, ForwardRef, Sequence, get_args, get_origin

import httpx
import pytest
from bs4 import BeautifulSoup

import telegram
from telegram._utils.defaultvalue import DefaultValue
from telegram._utils.types import DVInput, FileInput, ODVInput, ReplyMarkup
from tests.auxil.envvars import env_var_2_bool

IGNORED_OBJECTS = ("ResponseParameters", "CallbackGame")
GLOBALLY_IGNORED_PARAMETERS = {
    "self",
    "read_timeout",
    "write_timeout",
    "connect_timeout",
    "pool_timeout",
    "bot",
    "api_kwargs",
}

# Arguments *added* to the official API
PTB_EXTRA_PARAMS = {
    "send_contact": {"contact"},
    "send_location": {"location"},
    "edit_message_live_location": {"location"},
    "send_venue": {"venue"},
    "answer_inline_query": {"current_offset"},
    "send_media_group": {"caption", "parse_mode", "caption_entities"},
    "send_(animation|audio|document|photo|video(_note)?|voice)": {"filename"},
    "InlineQueryResult": {"id", "type"},  # attributes common to all subclasses
    "ChatMember": {"user", "status"},  # attributes common to all subclasses
    "BotCommandScope": {"type"},  # attributes common to all subclasses
    "MenuButton": {"type"},  # attributes common to all subclasses
    "PassportFile": {"credentials"},
    "EncryptedPassportElement": {"credentials"},
    "PassportElementError": {"source", "type", "message"},
    "InputMedia": {"caption", "caption_entities", "media", "media_type", "parse_mode"},
    "InputMedia(Animation|Audio|Document|Photo|Video|VideoNote|Voice)": {"filename"},
    "InputFile": {"attach", "filename", "obj"},
}


def _get_params_base(object_name: str, search_dict: dict[str, set[str]]) -> set[str]:
    """Helper function for the *_params functions below.
    Given an object name and a search dict, goes through the keys of the search dict and checks if
    the object name matches any of the regexes (keys). The union of all the sets (values) of the
    matching regexes is returned. `object_name` may be a CamelCase or snake_case name.
    """
    out = set()
    for regex, params in search_dict.items():
        if re.fullmatch(regex, object_name):
            out.update(params)
        # also check the snake_case version
        snake_case_name = re.sub(r"(?<!^)(?=[A-Z])", "_", object_name).lower()
        if re.fullmatch(regex, snake_case_name):
            out.update(params)
    return out


def ptb_extra_params(object_name) -> set[str]:
    return _get_params_base(object_name, PTB_EXTRA_PARAMS)


# Arguments *removed* from the official API
PTB_IGNORED_PARAMS = {
    r"InlineQueryResult\w+": {"type"},
    r"ChatMember\w+": {"status"},
    r"PassportElementError\w+": {"source"},
    "ForceReply": {"force_reply"},
    "ReplyKeyboardRemove": {"remove_keyboard"},
    r"BotCommandScope\w+": {"type"},
    r"MenuButton\w+": {"type"},
    r"InputMedia\w+": {"type"},
}


def ptb_ignored_params(object_name) -> set[str]:
    return _get_params_base(object_name, PTB_IGNORED_PARAMS)


IGNORED_PARAM_REQUIREMENTS = {
    # Ignore these since there's convenience params in them (eg. Venue)
    # <----
    "send_location": {"latitude", "longitude"},
    "edit_message_live_location": {"latitude", "longitude"},
    "send_venue": {"latitude", "longitude", "title", "address"},
    "send_contact": {"phone_number", "first_name"},
    # ---->
}


def ignored_param_requirements(object_name) -> set[str]:
    return _get_params_base(object_name, IGNORED_PARAM_REQUIREMENTS)


# Arguments that are optional arguments for now for backwards compatibility
BACKWARDS_COMPAT_KWARGS = {}


def backwards_compat_kwargs(object_name: str) -> set[str]:
    return _get_params_base(object_name, BACKWARDS_COMPAT_KWARGS)


IGNORED_PARAM_REQUIREMENTS.update(BACKWARDS_COMPAT_KWARGS)


def find_next_sibling_until(tag, name, until):
    for sibling in tag.next_siblings:
        if sibling is until:
            return None
        if sibling.name == name:
            return sibling
    return None


def parse_table(h4) -> list[list[str]]:
    """Parses the Telegram doc table and has an output of a 2D list."""
    table = find_next_sibling_until(h4, "table", h4.find_next_sibling("h4"))
    if not table:
        return []
    return [[td.text for td in tr.find_all("td")] for tr in table.find_all("tr")[1:]]


def check_method(h4):
    name = h4.text  # name of the method in telegram's docs.
    method = getattr(telegram.Bot, name, False)  # Retrieve our lib method
    if not method:
        raise AssertionError(f"Method {name} not found in telegram.Bot")

    table = parse_table(h4)

    # Check arguments based on source
    sig = inspect.signature(method, follow_wrapped=True)
    checked = []
    for tg_parameter in table:  # Iterates through each row in the table
        # Check if parameter is present in our method
        param = sig.parameters.get(
            tg_parameter[0]  # parameter[0] is first element (the param name)
        )
        if param is None:
            raise AssertionError(f"Parameter {tg_parameter[0]} not found in {method.__name__}")

        # Check if type annotation is present and correct
        if param.annotation is inspect.Parameter.empty:
            raise AssertionError(
                f"Param {param.name!r} of {method.__name__!r} should have a type annotation"
            )
        if not check_param_type(param, tg_parameter, name):
            raise AssertionError(
                f"Param {param.name!r} of {method.__name__!r} should be {tg_parameter[1]}"
            )

        # Now check if the parameter is required or not
        if not check_required_param(tg_parameter, param, method.__name__):
            raise AssertionError(
                f"Param {param.name!r} of method {method.__name__!r} requirement mismatch!"
            )

        # Now we will check that we don't pass default values if the parameter is not required.
        if param.default is not inspect.Parameter.empty:  # If there is a default argument...
            default_arg_none = check_defaults_type(param)  # check if it's None
            if not default_arg_none:
                raise AssertionError(f"Param {param.name!r} of {method.__name__!r} should be None")
        checked.append(tg_parameter[0])

    expected_additional_args = GLOBALLY_IGNORED_PARAMETERS.copy()
    expected_additional_args |= ptb_extra_params(name)
    expected_additional_args |= backwards_compat_kwargs(name)

    unexpected_args = (sig.parameters.keys() ^ checked) - expected_additional_args
    if unexpected_args != set():
        raise AssertionError(
            f"In {method.__qualname__}, unexpected args were found: {unexpected_args}."
        )

    kw_or_positional_args = [
        p.name for p in sig.parameters.values() if p.kind != inspect.Parameter.KEYWORD_ONLY
    ]
    non_kw_only_args = set(kw_or_positional_args).difference(checked).difference(["self"])
    non_kw_only_args -= backwards_compat_kwargs(name)
    if non_kw_only_args != set():
        raise AssertionError(
            f"In {method.__qualname__}, extra args should be keyword only "
            f"(compared to {name} in API)"
        )


def check_object(h4):
    name = h4.text
    obj = getattr(telegram, name)
    table = parse_table(h4)

    # Check arguments based on source. Makes sure to only check __init__'s signature & nothing else
    sig = inspect.signature(obj.__init__, follow_wrapped=True)

    checked = set()
    fields_removed_by_ptb = ptb_ignored_params(name)
    for tg_parameter in table:
        field: str = tg_parameter[0]  # From telegram docs

        if field in fields_removed_by_ptb:
            continue

        if field == "from":
            field = "from_user"

        param = sig.parameters.get(field)
        if param is None:
            raise AssertionError(f"Attribute {field} not found in {obj.__name__}")
        # TODO: Check type via docstring
        if not check_required_param(tg_parameter, param, obj.__name__):
            raise AssertionError(f"{obj.__name__!r} parameter {param.name!r} requirement mismatch")

        if param.default is not inspect.Parameter.empty:  # If there is a default argument...
            default_arg_none = check_defaults_type(param)  # check if its None
            if not default_arg_none:
                raise AssertionError(f"Param {param.name!r} of {obj.__name__!r} should be `None`")

        checked.add(field)

    expected_additional_args = GLOBALLY_IGNORED_PARAMETERS.copy()
    expected_additional_args |= ptb_extra_params(name)
    expected_additional_args |= backwards_compat_kwargs(name)

    unexpected_args = (sig.parameters.keys() ^ checked) - expected_additional_args
    if unexpected_args != set():
        raise AssertionError(f"In {name}, unexpected args were found: {unexpected_args}.")


def is_parameter_required_by_tg(field: str) -> bool:
    if field in {"Required", "Yes"}:
        return True
    return field.split(".", 1)[0] != "Optional"  # splits the sentence and extracts first word


def check_required_param(
    param_desc: list[str], param: inspect.Parameter, method_or_obj_name: str
) -> bool:
    """Checks if the method/class parameter is a required/optional param as per Telegram docs.

    Returns:
        :obj:`bool`: The boolean returned represents whether our parameter's requirement (optional
        or required) is the same as Telegram's or not.
    """
    is_ours_required = param.default is inspect.Parameter.empty
    telegram_requires = is_parameter_required_by_tg(param_desc[2])
    # Handle cases where we provide convenience intentionally-
    if param.name in ignored_param_requirements(method_or_obj_name):
        return True
    return telegram_requires is is_ours_required


def check_defaults_type(ptb_param: inspect.Parameter) -> bool:
    return DefaultValue.get_value(ptb_param.default) is None


def check_param_type(ptb_param: inspect.Parameter, tg_parameter: list[str], name: str) -> bool:
    """This function checks whether the type annotation of the parameter is the same as the one
    specified in the official API. It also checks for some special cases where we accept more types

    Args:
        ptb_param (inspect.Parameter): The parameter object from our methods/classes
        tg_parameter (list[str]): The table row corresponding to the parameter from official API.
        name (str): The name of the method/class

    Returns:
        :obj:`bool`: The boolean returned represents whether our parameter's type annotation is the
        same as Telegram's or not.
    """
    # In order to evaluate the type annotation, we need to first have a mapping of the types
    # specified in the official API to our types. The keys are types in the column of official API.
    TYPE_MAPPING = {
        "Integer or String": {int | str},
        "Integer": {int},
        "String": {str},
        "Boolean": {bool},
        "Float number": {float},
        r"Array of [\w\,\s]*": {Sequence},
        r"Array of Array of [\w\,\s]*": {Sequence, Sequence},
        "InlineKeyboardMarkup or ReplyKeyboardMarkup or ReplyKeyboardRemove or ForceReply": {
            ReplyMarkup
        },
        r"InputFile(?: or String)?": {FileInput},
    }

    tg_param_type = tg_parameter[1]  # Type of parameter as specified in the docs
    mapped: set[type] = _get_params_base(tg_param_type, TYPE_MAPPING)
    if len(mapped) == 0:
        # We want to store both string version of class and the class obj itself. e.g. "InputMedia"
        # and InputMedia because some annotations might be ForwardRefs.
        mapped_type: list = [
            getattr(telegram, tg_param_type),
            ForwardRef(tg_param_type),
            tg_param_type,
        ]
    elif len(mapped) == 1:
        mapped_type: set = mapped.pop()
    else:
        mapped_type = mapped
        # print("SHOULDNOT REACH HERE")

    # Resolve nested annotations to get inner types.
    if (ptb_annotation := list(get_args(ptb_param.annotation))) == []:
        ptb_annotation = ptb_param.annotation  # if it's not nested, just use the annotation

    # print(f"{tg_param_type=} ||| {mapped_type=} ||| {ptb_annotation=}")

    if isinstance(ptb_annotation, list):
        # Some cleaning:
        # Remove 'Optional[...]' from the annotation if it's present. We do it this way since: 1)
        # we already check if argument should be optional or not + type checkers will complain.
        # 2) we don't want to deal with Optional[..] skewing the equality check of our annotation
        # with the official API, i.e. Optional[int] == int.
        if type(None) in ptb_annotation:
            print(f"found None in {ptb_param.name}")
            ptb_annotation.remove(type(None))

        # Cleaning done... now let's put it back together.
        # Join all the annotations back (i.e. Union)
        ptb_annotation = _unionizer(ptb_annotation, False)

        # Last step, we need to use get_origin to get the original type, since using get_args
        # above will strip that out.
        wrapped = get_origin(ptb_param.annotation)
        if wrapped is not None:
            # collections.abc.Sequence -> typing.Sequence
            if "collections.abc.Sequence" in str(wrapped):
                wrapped = Sequence
            ptb_annotation = wrapped[ptb_annotation]
        # We have put back our annotation together after removing the NoneType!

    # Now let's do the checking
    # Loose type checking for "Array of " annotations for now. TODO: make it strict later
    if "Array of " in tg_param_type:
        # print("array of ", ptb_param.annotation)

        # For exceptions just check if they contain the annotation
        array_of_exceptions = {
            "results": "InlineQueryResult",
            "commands": "BotCommand",
        }
        if ptb_param.name in array_of_exceptions:
            return array_of_exceptions[ptb_param.name] in str(ptb_annotation)

        # let's import obj
        pattern = r"Array of(?: Array of)? ([\w\,\s]*)"  # matches "Array of [Array of] [obj]"
        obj_str: str = re.search(pattern, tg_param_type).group(1)  # extract obj from string
        # is obj a regular type like str?
        array_of_mapped: set[type] = _get_params_base(obj_str, TYPE_MAPPING)

        if len(array_of_mapped) == 0:  # no match found, it's from telegram module
            # print('from tg module')
            # it could be a list of objects, so let's check that:
            objs = _extract_words(obj_str)
            # let's unionize all the objects
            obj = _unionizer(objs, True)
        else:
            print("from TYPE_MAPPING")
            obj = array_of_mapped.pop()

        # This means it is Array of [obj]
        if isinstance(mapped_type, type) or "Sequence" in str(mapped_type):
            # print('isinstance', mapped_type[obj], ptb_annotation)
            return mapped_type[obj] == ptb_annotation
        # This means it is Array of Array of [obj]
        if len(mapped_type) == 1:
            ele = mapped_type.pop()
            seq_1: Sequence = ele[0]
            seq_2: Sequence = ele[1]
            # print('isinstance 2', seq_1[seq_2[obj]], ptb_annotation)
            return seq_1[seq_2[obj]] == ptb_annotation

    DEFAULT_VAL_PARAMS = {
        "parse_mode",
        "disable_notification",
        "allow_sending_without_reply",
        "protect_content",
        "disable_web_page_preview",
        "explanation_parse_mode",
    }

    # Special case for when the parameter is a default value parameter
    if ptb_param.name in DEFAULT_VAL_PARAMS:
        # Check if it's DVInput or ODVInput
        for param_type in [DVInput, ODVInput]:
            parsed = param_type[mapped_type]
            if ptb_annotation == parsed:
                return True
        # print(f"cause {mapped_type} != {ptb_annotation} for {ptb_param.name}")
        return False

    # Special case for send_* methods where we accept more types than the official API:
    additional_types = {
        "photo": ForwardRef("PhotoSize"),
        "video": ForwardRef("Video"),
        "video_note": ForwardRef("VideoNote"),
        "audio": ForwardRef("Audio"),
        "document": ForwardRef("Document"),
        "animation": ForwardRef("Animation"),
        "voice": ForwardRef("Voice"),
        "sticker": ForwardRef("Sticker"),
    }

    if (
        ptb_param.name in additional_types
        and not isinstance(mapped_type, list)
        and name.startswith("send")
    ):
        print("additional_types ")
        mapped_type = mapped_type | additional_types[ptb_param.name]

    # Special cases for other methods that accept more types than the official API
    exceptions = {  # param_name: reduced form of annotation
        "correct_option_id": int,
        "file_id": str,
        "invite_link": str,
        "provider_data": str,
    }

    if ptb_param.name in exceptions:
        ptb_annotation = exceptions[ptb_param.name]

    # Special case for datetimes
    if re.search(r"([_]+|\b)date[^\w]*\b", ptb_param.name):
        mapped_type = mapped_type | datetime

    # Final check for the basic types
    if isinstance(mapped_type, list) and any(ptb_annotation == t for t in mapped_type):
        return True

    if mapped_type != ptb_annotation:
        # print(f"cause {mapped_type} != {ptb_annotation} for {ptb_param.name}")
        return False

    return True


def _extract_words(text: str) -> set[str]:
    """Extracts all words from a string, removing all punctuation and words like 'and'."""
    return set(re.sub(r"[^\w\s]", "", text).split()) - {"and"}


def _unionizer(annotation: Sequence, forward_ref: bool) -> Any:
    """Returns a union of all the types in the annotation. If forward_ref is True, it wraps the
    annotation in a ForwardRef and then unionizes."""
    union = None
    for t in annotation:
        if forward_ref:
            t = ForwardRef(t)  # noqa: PLW2901
        union = t if union is None else union | t
    return union


to_run = env_var_2_bool(os.getenv("TEST_OFFICIAL"))
argvalues = []
names = []

if to_run:
    argvalues = []
    names = []
    request = httpx.get("https://core.telegram.org/bots/api")
    soup = BeautifulSoup(request.text, "html.parser")

    for thing in soup.select("h4 > a.anchor"):
        # Methods and types don't have spaces in them, luckily all other sections of the docs do
        # TODO: don't depend on that
        if "-" not in thing["name"]:
            h4 = thing.parent

            # Is it a method
            if h4.text[0].lower() == h4.text[0]:
                argvalues.append((check_method, h4))
                names.append(h4.text)
            elif h4.text not in IGNORED_OBJECTS:  # Or a type/object
                argvalues.append((check_object, h4))
                names.append(h4.text)


@pytest.mark.skipif(not to_run, reason="test_official is not enabled")
@pytest.mark.parametrize(("method", "data"), argvalues=argvalues, ids=names)
def test_official(method, data):
    method(data)
