"""
filter_base is used to provide common filter functionality for directives
like needtable, needlist and needflow.
"""

from __future__ import annotations

import re
from timeit import default_timer as timer
from types import CodeType
from typing import Any, Iterable, TypedDict, TypeVar

from docutils import nodes
from docutils.parsers.rst import directives
from sphinx.application import Sphinx
from sphinx.util.docutils import SphinxDirective

from sphinx_needs.api.exceptions import NeedsInvalidFilter
from sphinx_needs.config import NeedsSphinxConfig
from sphinx_needs.data import (
    NeedsFilteredBaseType,
    NeedsInfoType,
    NeedsView,
    SphinxNeedsData,
)
from sphinx_needs.debug import measure_time, measure_time_func
from sphinx_needs.logging import log_warning
from sphinx_needs.roles.need_part import iter_need_parts
from sphinx_needs.utils import check_and_get_external_filter_func
from sphinx_needs.utils import logger as log


class FilterAttributesType(TypedDict):
    status: list[str]
    tags: list[str]
    types: list[str]
    filter: str
    sort_by: str
    filter_code: list[str]
    filter_func: str | None
    export_id: str
    filter_warning: str | None
    """If set, the filter is exported with this ID in the needs.json file."""


class FilterBase(SphinxDirective):
    has_content = True

    base_option_spec = {
        "status": directives.unchanged_required,
        "tags": directives.unchanged_required,
        "types": directives.unchanged_required,
        "filter": directives.unchanged_required,
        "filter-func": directives.unchanged_required,
        "sort_by": directives.unchanged,
        "export_id": directives.unchanged,
        "filter_warning": directives.unchanged,
    }

    def collect_filter_attributes(self) -> FilterAttributesType:
        _tags = str(self.options.get("tags", ""))
        tags = (
            [tag.strip() for tag in re.split(";|,", _tags) if len(tag) > 0]
            if _tags
            else []
        )

        status = self.options.get("status")
        if status:
            try:
                status = str(status)
                status = [stat.strip() for stat in re.split(";|,", status)]
            except Exception:
                # If we could not transform/use status information, we just skip this status
                pass
        else:
            status = []

        types = self.options.get("types", [])
        if isinstance(types, str):
            types = [typ.strip() for typ in re.split(";|,", types)]

        if self.options.get("export_id", ""):
            # this is used by needs builders
            SphinxNeedsData(self.env).has_export_filters = True

        # Add the need and all needed information
        collected_filter_options: FilterAttributesType = {
            "status": status,
            "tags": tags,
            "types": types,
            "filter": self.options.get("filter"),
            "sort_by": self.options.get("sort_by"),
            "filter_code": self.content,
            "filter_func": self.options.get("filter-func"),
            "export_id": self.options.get("export_id", ""),
            "filter_warning": self.options.get("filter_warning"),
        }
        return collected_filter_options


def process_filters(
    app: Sphinx,
    needs_view: NeedsView,
    filter_data: NeedsFilteredBaseType,
    origin: str,
    location: nodes.Element,
    include_external: bool = True,
) -> list[NeedsInfoType]:
    """
    Filters all needs with given configuration.
    Used by needlist, needtable and needflow.

    :param app: Sphinx application object
    :param filter_data: Filter configuration
    :param origin: Origin of the request (e.g. needlist, needtable, needflow)
    :param location: Origin node of the request
    :param include_external: Boolean, which decides to include external needs or not

    :return: list of needs, which passed the filters
    """
    start = timer()
    needs_config = NeedsSphinxConfig(app.config)
    found_needs: list[NeedsInfoType]

    # check if include external needs
    all_needs: Iterable[NeedsInfoType] = needs_view.values()
    if not include_external:
        all_needs = [need for need in all_needs if not need["is_external"]]

    found_needs_by_options: list[NeedsInfoType] = []

    # Add all need_parts of given needs to the search list
    all_needs_incl_parts = prepare_need_list(all_needs)

    # Check if external filter code is defined
    try:
        filter_func_sig = check_and_get_external_filter_func(
            filter_data.get("filter_func")
        )
    except NeedsInvalidFilter as e:
        log_warning(
            log,
            str(e),
            "filter_func",
            location=location,
        )
        return []

    filter_code = None
    # Get filter_code from
    if not filter_code and filter_data["filter_code"]:
        filter_code = "\n".join(filter_data["filter_code"])

    if (not filter_code or filter_code.isspace()) and not filter_func_sig:
        if bool(filter_data["status"] or filter_data["tags"] or filter_data["types"]):
            for need_info in all_needs_incl_parts:
                status_filter_passed = False
                if (
                    not filter_data["status"]
                    or need_info["status"]
                    and need_info["status"] in filter_data["status"]
                ):
                    # Filtering for status was not requested or match was found
                    status_filter_passed = True

                tags_filter_passed = False
                if (
                    len(set(need_info["tags"]) & set(filter_data["tags"])) > 0
                    or len(filter_data["tags"]) == 0
                ):
                    tags_filter_passed = True

                type_filter_passed = False
                if (
                    need_info["type"] in filter_data["types"]
                    or need_info["type_name"] in filter_data["types"]
                    or len(filter_data["types"]) == 0
                ):
                    type_filter_passed = True

                if status_filter_passed and tags_filter_passed and type_filter_passed:
                    found_needs_by_options.append(need_info)
            # Get need by filter string
            found_needs_by_string = filter_needs(
                all_needs_incl_parts,
                needs_config,
                filter_data["filter"],
                location=location,
            )
            # Make an intersection of both lists
            found_needs = intersection_of_need_results(
                found_needs_by_options, found_needs_by_string
            )
        else:
            # There is no other config as the one for filter string.
            # So we only need this result.
            found_needs = filter_needs(
                all_needs_incl_parts,
                needs_config,
                filter_data["filter"],
                location=location,
            )
    else:
        # Provides only a copy of needs to avoid data manipulations.
        context: dict[str, Any] = {
            "needs": all_needs_incl_parts,
            "results": [],
        }

        if filter_code:  # code from content
            exec(filter_code, context)
        elif filter_func_sig:  # code from external file
            args = []
            if filter_func_sig.args:
                args = filter_func_sig.args.split(",")
            for index, arg in enumerate(args):
                # All args are strings, but we must transform them to requested type, e.g. 1 -> int, "1" -> str
                context[f"arg{index+1}"] = arg

            # Decorate function to allow time measurments
            filter_func = measure_time_func(
                filter_func_sig.func, category="filter_func", source="user"
            )
            filter_func(**context)
        else:
            log_warning(
                log, "Something went wrong running filter", None, location=location
            )
            return []

        # The filter results may be dirty, as it may continue manipulated needs.
        found_dirty_needs: list[NeedsInfoType] = context["results"]
        found_needs = []

        # Check if config allow unsafe filters
        if needs_config.allow_unsafe_filters:
            found_needs = found_dirty_needs
        else:
            # Just take the ids from search result and use the related, but original need
            found_need_ids = [x["id_complete"] for x in found_dirty_needs]
            for need in all_needs_incl_parts:
                if need["id_complete"] in found_need_ids:
                    found_needs.append(need)

    if sort_key := filter_data["sort_by"]:
        try:
            found_needs = sorted(
                found_needs,
                key=lambda node: node[sort_key] or "",  # type: ignore[literal-required]
            )
        except KeyError as e:
            log_warning(
                log,
                f"Sorting parameter {sort_key} not valid: Error: {e}",
                None,
                location=location,
            )
            return []

    # Store basic filter configuration and result global list.
    # Needed mainly for exporting the result to needs.json (if builder "needs" is used).
    filter_list = SphinxNeedsData(app.env).get_or_create_filters()

    filter_list[filter_data["target_id"]] = {
        "origin": origin,
        "location": f"{location.source}:{location.line}",
        "filter": filter_data["filter"] or "",
        "status": filter_data["status"],
        "tags": filter_data["tags"],
        "types": filter_data["types"],
        "export_id": filter_data["export_id"].upper(),
        "amount": len(found_needs),
        "runtime": timer() - start,
    }

    return found_needs


def prepare_need_list(need_list: Iterable[NeedsInfoType]) -> list[NeedsInfoType]:
    # all_needs_incl_parts = need_list.copy()
    all_needs_incl_parts: list[NeedsInfoType]
    try:
        all_needs_incl_parts = need_list[:]  # type: ignore
    except TypeError:
        try:
            all_needs_incl_parts = need_list.copy()  # type: ignore
        except AttributeError:
            all_needs_incl_parts = list(need_list)[:]

    for need in need_list:
        for filter_part in iter_need_parts(need):
            all_needs_incl_parts.append(filter_part)

    return all_needs_incl_parts


T = TypeVar("T")


def intersection_of_need_results(list_a: list[T], list_b: list[T]) -> list[T]:
    return [a for a in list_a if a in list_b]


V = TypeVar("V", bound=NeedsInfoType)


@measure_time("filtering")
def filter_needs(
    needs: Iterable[V],
    config: NeedsSphinxConfig,
    filter_string: None | str = "",
    current_need: NeedsInfoType | None = None,
    *,
    location: tuple[str, int | None] | nodes.Node | None = None,
    append_warning: str = "",
) -> list[V]:
    """
    Filters given needs based on a given filter string.
    Returns all needs, which pass the given filter.

    :param needs: list of needs, which shall be filtered
    :param config: NeedsSphinxConfig object
    :param filter_string: strings, which gets evaluated against each need
    :param current_need: current need, which uses the filter.
    :param location: source location for error reporting (docname, line number)
    :param append_warning: additional text to append to any failed filter warning

    :return: list of found needs
    """
    if not filter_string:
        return list(needs)

    found_needs = []

    # https://docs.python.org/3/library/functions.html?highlight=compile#compile
    filter_compiled = compile(filter_string, "<string>", "eval")
    error_reported = False
    for filter_need in needs:
        try:
            if filter_single_need(
                filter_need,
                config,
                filter_string,
                needs,
                current_need,
                filter_compiled=filter_compiled,
            ):
                found_needs.append(filter_need)
        except Exception as e:
            if not error_reported:  # Let's report a filter-problem only once
                if append_warning:
                    append_warning = f" {append_warning}"
                log_warning(
                    log,
                    f"{e}{append_warning}",
                    "filter",
                    location=location,
                )
                error_reported = True

    return found_needs


def need_search(*args: Any, **kwargs: Any) -> bool:
    return re.search(*args, **kwargs) is not None


@measure_time("filtering")
def filter_single_need(
    need: NeedsInfoType,
    config: NeedsSphinxConfig,
    filter_string: str = "",
    needs: Iterable[NeedsInfoType] | None = None,
    current_need: NeedsInfoType | None = None,
    filter_compiled: CodeType | None = None,
) -> bool:
    """
    Checks if a single need/need_part passes a filter_string

    :param need: the data for a single need
    :param config: NeedsSphinxConfig object
    :param filter_compiled: An already compiled filter_string to safe time
    :param need: need or need_part
    :param filter_string: string, which is used as input for eval()
    :param needs: list of all needs
    :return: True, if need passes the filter_string, else False
    """
    filter_context: dict[str, Any] = need.copy()  # type: ignore
    if needs:
        filter_context["needs"] = needs
    if current_need:
        filter_context["current_need"] = current_need
    else:
        filter_context["current_need"] = need

    # Get needs external filter data and merge to filter_context
    filter_context.update(config.filter_data)

    filter_context["search"] = need_search
    result = False
    try:
        # Set filter_context as globals and not only locals in eval()!
        # Otherwise, the vars not be accessed in list comprehensions.
        if filter_compiled:
            result = eval(filter_compiled, filter_context)
        else:
            result = eval(filter_string, filter_context)
        if not isinstance(result, bool):
            raise NeedsInvalidFilter(
                f"Filter did not evaluate to a boolean, instead {type(result)}: {result}"
            )
    except Exception as e:
        raise NeedsInvalidFilter(f"Filter {filter_string!r} not valid. Error: {e}.")
    return result
