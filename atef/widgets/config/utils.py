from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import fields
from enum import IntEnum
from functools import partial
from itertools import zip_longest
from typing import (Any, Callable, ClassVar, Dict, List, Optional, Tuple, Type,
                    Union)

import qtawesome as qta
from ophyd import EpicsSignal, EpicsSignalRO
from qtpy import QtCore, QtWidgets
from qtpy.QtCore import (QPoint, QPointF, QRect, QRectF, QRegularExpression,
                         QSize, Qt, QTimer)
from qtpy.QtCore import Signal as QSignal
from qtpy.QtGui import (QBrush, QClipboard, QColor, QGuiApplication, QPainter,
                        QPaintEvent, QPen, QRegularExpressionValidator)
from qtpy.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QInputDialog,
                            QLabel, QLayout, QLineEdit, QMenu, QPushButton,
                            QSizePolicy, QSpinBox, QStyle, QToolButton,
                            QWidget)

from atef import util
from atef.cache import DataCache, get_signal_cache
from atef.check import Comparison, EpicsValue, Equals, HappiValue, Range
from atef.config import (Configuration, DeviceConfiguration,
                         PreparedComparison, PreparedConfiguration,
                         PreparedFile, PVConfiguration, ToolConfiguration)
from atef.enums import Severity
from atef.exceptions import DynamicValueError, MissingHappiDeviceError
from atef.procedure import (ProcedureFile, ProcedureStep, SetValueStep,
                            walk_steps)
from atef.qt_helpers import (QDataclassBridge, QDataclassList, QDataclassValue,
                             ThreadWorker)
from atef.result import combine_results, incomplete_result
from atef.tools import Ping
from atef.type_hints import Number
from atef.widgets.archive_viewer import get_archive_viewer
from atef.widgets.core import DesignerDisplay
from atef.widgets.happi import HappiDeviceComponentWidget
from atef.widgets.ophyd import OphydAttributeData, OphydAttributeDataSummary
from atef.widgets.utils import (BusyCursorThread, PV_validator,
                                match_line_edit_text_width)

logger = logging.getLogger(__name__)


class StringListWithDialog(DesignerDisplay, QWidget):
    """
    A widget used to modify the str variant of QDataclassList, tied to a
    specific dialog that helps with selection of strings.

    The ``item_add_request`` signal must be hooked into with the
    caller-specific dialog tool.  This class may be subclassed to add this
    functionality.

    Parameters
    ----------
    data_list : QDataclassList
        The dataclass list to edit using this widget.

    allow_duplicates : bool, optional
        Allow duplicate entries in the list.  Defaults to False.
    """
    filename: ClassVar[str] = "string_list_with_dialog.ui"
    item_add_request: ClassVar[QSignal] = QSignal()
    item_edit_request: ClassVar[QSignal] = QSignal(list)  # List[str]

    button_add: QtWidgets.QToolButton
    button_layout: QtWidgets.QVBoxLayout
    button_remove: QtWidgets.QToolButton
    list_strings: QtWidgets.QListWidget

    def __init__(
        self,
        data_list: QDataclassList,
        allow_duplicates: bool = False,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.data_list = data_list
        self.allow_duplicates = allow_duplicates
        self._setup_ui()

    def _setup_ui(self) -> None:
        starting_list = self.data_list.get()
        for starting_value in starting_list or []:
            self._add_item(starting_value, init=True)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        # def test():
        #     text, success = QtWidgets.QInputDialog.getText(
        #         self, "Device name", "Device name?"
        #     )
        #     if success:
        #         self.add_items([item for item in text.strip().split() if item])

        self.button_add.clicked.connect(self.item_add_request.emit)
        self.button_remove.clicked.connect(self._remove_item_request)

        def _edit_item_request():
            self.item_edit_request.emit(self.selected_items_text)

        self.list_strings.doubleClicked.connect(_edit_item_request)

    def _add_item(self, item: str, *, init: bool = False):
        """
        Add an item to the QListWidget and the bridge (if init is not set).

        Parameters
        ----------
        item : str
            The item to add.

        init : bool, optional
            Whether or not this is the initial initialization of this widget.
            This will be set to True in __init__ so that we don't mutate
            the underlying dataclass. False, the default, means that we're
            adding a new dataclass to the list, which means we should
            definitely append it.
        """
        if not init:
            if not self.allow_duplicates and item in self.data_list.get():
                return

            self.data_list.append(item)

        self.list_strings.addItem(QtWidgets.QListWidgetItem(item))

    def add_items(self, items: List[str]) -> None:
        """
        Add one or more strings to the QListWidget and the bridge.

        Parameters
        ----------
        item : list of str
            The item(s) to add.
        """
        for item in items:
            self._add_item(item)

    @property
    def selected_items_text(self) -> List[str]:
        """
        The text of item(s) currently selected in the QListWidget.

        Returns
        -------
        selected : list of str
        """
        return [item.text() for item in list(self.list_strings.selectedItems())]

    def _remove_item_request(self):
        """Qt hook: user requested item removal."""
        for item in self.list_strings.selectedItems():
            self.data_list.remove_value(item.text())
            self.list_strings.takeItem(self.list_strings.row(item))

    def _remove_item(self, item: str) -> None:
        """
        Remove an item from the QListWidget and the bridge.

        Parameters
        ----------
        items : str
            The item to remove.
        """
        self.data_list.remove_value(item)
        for row in range(self.list_strings.count()):
            if self.list_strings.item(row).text() == item:
                self.list_strings.takeItem(row)
                return

    def remove_items(self, items: List[str]) -> None:
        """
        Remove items from the QListWidget and the bridge.

        Parameters
        ----------
        items : list of str
            The items to remove.
        """
        for item in items:
            self._remove_item(item)

    def _edit_item(self, old: str, new: str) -> None:
        """
        Edit an item in place in the QListWidget and the bridge.

        If we don't allow duplicates and new already exists, we
        need to remove old instead.

        Parameters
        ----------
        old : str
            The original item to replace
        new : str
            The new item to replace it with
        """
        if old == new:
            return
        if not self.allow_duplicates and new in self.data_list.get():
            return self._remove_item(old)
        self.data_list.put_to_index(
            index=self.data_list.get().index(old),
            new_value=new,
        )
        for row in range(self.list_strings.count()):
            if self.list_strings.item(row).text() == old:
                self.list_strings.item(row).setText(new)
                return

    def edit_items(self, old_items: List[str], new_items: List[str]) -> None:
        """
        Best-effort edit of items in place in the QListWidget and the bridge.

        The goal is to replace each instance of old with each instance of
        new, in order.
        """
        # Ignore items that exist in both lists
        old_uniques = [item for item in old_items if item not in new_items]
        new_uniques = [item for item in new_items if item not in old_items]
        # Remove items from new if duplicates aren't allowed and they exist
        if not self.allow_duplicates:
            new_uniques = [
                item for item in new_uniques if item not in self.data_list.get()
            ]
        # Add, remove, edit in place as necessary
        # This will edit everything in place if the lists are equal length
        # If old_uniques is longer, we'll remove when we exhaust new_uniques
        # If new_uniques is longer, we'll add when we exhaust old_uniques
        # TODO find a way to add these at the selected index
        for old, new in zip_longest(old_uniques, new_uniques, fillvalue=None):
            if old is None:
                self._add_item(new)
            elif new is None:
                self._remove_item(old)
            else:
                self._edit_item(old, new)

    def _show_context_menu(self, pos: QPoint) -> None:
        """
        Displays a context menu that provides copy & remove actions
        to the user

        Parameters
        ----------
        pos : QPoint
            Position to display the menu at
        """
        if len(self.list_strings.selectedItems()) <= 0:
            return

        menu = QMenu(self)

        def copy_selected():
            items = self.list_strings.selectedItems()
            text = '\n'.join([x.text() for x in items])
            if len(text) > 0:
                QGuiApplication.clipboard().setText(text, QClipboard.Mode.Clipboard)

        copy = menu.addAction('&Copy')
        copy.triggered.connect(copy_selected)

        remove = menu.addAction('&Remove')
        remove.triggered.connect(self._remove_item_request)

        menu.exec(self.mapToGlobal(pos))


class DeviceListWidget(StringListWithDialog):
    """
    Device list widget, with ``HappiSearchWidget`` for adding new devices.
    """

    _search_widget: Optional[HappiDeviceComponentWidget] = None

    def _setup_ui(self) -> None:
        super()._setup_ui()
        self.item_add_request.connect(self._open_device_chooser)
        self.item_edit_request.connect(self._open_device_chooser)

    def _open_device_chooser(self, to_select: Optional[List[str]] = None) -> None:
        """
        Hook: User requested adding/editing an existing device.

        Parameters
        ----------
        to_select : list of str, optional
            If provided, the device chooser will filter for these items.
        """
        self._search_widget = HappiDeviceComponentWidget(
            client=util.get_happi_client(),
            show_device_components=False,
        )
        self._search_widget.item_search_widget.happi_items_chosen.connect(
            self.add_items
        )
        self._search_widget.show()
        self._search_widget.activateWindow()
        self._search_widget.item_search_widget.edit_filter.setText(
            util.regex_for_devices(to_select)
        )


class ComponentListWidget(StringListWithDialog):
    """
    Component list widget using a ``HappiDeviceComponentWidget``.
    """

    _search_widget: Optional[HappiDeviceComponentWidget] = None
    suggest_comparison: QSignal = QSignal(Comparison)
    get_device_list: Optional[Callable[[], List[str]]]

    def __init__(
        self,
        data_list: QDataclassList,
        get_device_list: Optional[Callable[[], List[str]]] = None,
        allow_duplicates: bool = False,
        **kwargs,
    ):
        self.get_device_list = get_device_list
        super().__init__(data_list=data_list, allow_duplicates=allow_duplicates, **kwargs)

    def _setup_ui(self) -> None:
        super()._setup_ui()
        self.item_add_request.connect(self._open_component_chooser)
        self.item_edit_request.connect(self._open_component_chooser)

    def _open_component_chooser(self, to_select: Optional[List[str]] = None) -> None:
        """
        Hook: User requested adding/editing a component.

        Parameters
        ----------
        to_select : list of str, optional
            If provided, the device chooser will filter for these items.
        """

        widget = HappiDeviceComponentWidget(
            client=util.get_happi_client()
        )
        widget.device_widget.custom_menu_helper = self._attr_menu_helper
        self._search_widget = widget
        # widget.item_search_widget.happi_items_chosen.connect(
        #    self.add_items
        # )
        widget.show()
        widget.activateWindow()

        if self.get_device_list is not None:
            try:
                device_list = self.get_device_list()
            except Exception as ex:
                device_list = []
                logger.debug("Failed to get device list", exc_info=ex)

            widget.item_search_widget.edit_filter.setText(
                util.regex_for_devices(device_list)
            )

    def _attr_menu_helper(self, data: List[OphydAttributeData]) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu()

        summary = OphydAttributeDataSummary.from_attr_data(*data)
        short_attrs = [datum.attr.split(".")[-1] for datum in data]

        def add_attrs():
            for datum in data:
                self._add_item(datum.attr)

        def add_without():
            add_attrs()

        def add_with_equals():
            add_attrs()
            comparison = Equals(
                name=f'{"_".join(short_attrs)}_auto',
                description=f'Comparison from: {", ".join(short_attrs)}',
                value=summary.average,
            )
            self.suggest_comparison.emit(comparison)

        def add_with_range():
            add_attrs()
            comparison = Range(
                name=f'{"_".join(short_attrs)}_auto',
                description=f'Comparison from: {", ".join(short_attrs)}',
                low=summary.minimum,
                high=summary.maximum,
            )
            self.suggest_comparison.emit(comparison)

        def open_arch_viewer():
            arch_widget = get_archive_viewer()
            for datum in data:
                try:
                    parent_dev = (datum.signal.parent
                                  or datum.signal.biological_parent)
                    dev_attr = '.'.join((parent_dev.name, datum.attr))
                except Exception as e:
                    logger.debug('unable to resolve full device-attribute '
                                 f'string: {e}')
                    dev_attr = 'N/A'
                arch_widget.add_signal(
                    datum.pvname, dev_attr=dev_attr, update_curves=False
                )
                arch_widget.update_curves()
            arch_widget.show()

        menu.addSection("Open Archive Data viewer")
        archive_viewer_all = menu.addAction("View all selected in "
                                            "Archive Viewer")
        archive_viewer_all.triggered.connect(open_arch_viewer)

        menu.addSection("Add all selected")
        add_without_action = menu.addAction("Add selected without comparison")
        add_without_action.triggered.connect(add_without)

        if summary.average is not None:
            add_with_equals_action = menu.addAction(
                f"Add selected with Equals comparison (={summary.average})"
            )
            add_with_equals_action.triggered.connect(add_with_equals)

        if summary.minimum is not None:
            add_with_range_action = menu.addAction(
                f"Add selected with Range comparison "
                f"[{summary.minimum}, {summary.maximum}]"
            )
            add_with_range_action.triggered.connect(add_with_range)

        menu.addSection("Add single attribute")
        for attr in data:
            def add_single_attr(*, attr_name: str = attr.attr):
                self._add_item(attr_name)

            action = menu.addAction(f"Add {attr.attr}")
            action.triggered.connect(add_single_attr)

        return menu


class BulkListWidget(StringListWithDialog):
    """
    String list widget that uses a multi-line text box for entry and edit.
    """

    def _setup_ui(self) -> None:
        super()._setup_ui()
        self.item_add_request.connect(self._open_multiline)
        self.item_edit_request.connect(self._open_multiline)

    def _open_multiline(self, to_select: Optional[List[str]] = None) -> None:
        """
        User requested adding new strings or editing existing ones.

        Parameters
        ----------
        to_select : list of str, optional
            For editing, this will contain the string items that are
            selected so that we can pre-populate the edit box
            appropriately.
        """
        to_select = to_select or []
        if to_select:
            title = 'Edit PVs Dialog'
            label = 'Add to or edit these PVs as appropriate:'
            text = '\n'.join(to_select)
        else:
            title = 'Add PVs Dialog'
            label = 'Which PVs should be included?'
            text = ''
        user_input, ok = QInputDialog.getMultiLineText(
            self, title, label, text
        )
        if not ok:
            return
        new_pvs = [pv.strip() for pv in user_input.splitlines() if pv.strip()]
        self.edit_items(to_select, new_pvs)


class Toggle(QCheckBox):
    """
    A checkbox widget that looks like a sliding toggle. At default:
    - The disabled state displays the slider as grey and to the left.
    - The activated state displays the slider as blue and to the right
    """
    # shamelessly vendored from qtwidgets:
    # github.com/pythonguis/python-qtwidgets/tree/master/qtwidgets/toggle
    _transparent_pen = QPen(Qt.transparent)
    _light_grey_pen = QPen(Qt.lightGray)

    def __init__(
        self,
        *args,
        parent=None,
        bar_color=Qt.gray,
        checked_color="#00B0FF",
        handle_color=Qt.white,
        checked_icon='msc.run-all',
        unchecked_icon='fa5s.edit',
        **kwargs
    ):
        super().__init__(*args, parent=parent, **kwargs)
        # Save our properties on the object via self, so we can access them later
        # in the paintEvent.
        self.checked_color = checked_color
        self.checked_icon = checked_icon
        self.unchecked_icon = unchecked_icon
        self._bar_brush = QBrush(bar_color)
        self._bar_checked_brush = QBrush(QColor(checked_color).lighter())

        self._handle_brush = QBrush(handle_color)
        self._handle_checked_brush = QBrush(QColor(checked_color))

        # Setup the rest of the widget.

        self.setContentsMargins(0, 0, 0, 0)
        self._handle_position = 0

        self.stateChanged.connect(self.handle_state_change)

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(40, 25)

    def hitButton(self, pos: QPoint):
        return self.contentsRect().contains(pos)

    def paintEvent(self, e: QPaintEvent):
        contRect = self.contentsRect()
        handleRadius = round(0.45 * contRect.height())

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        p.setPen(self._transparent_pen)
        barRect = QRectF(
            0, 0,
            contRect.width() - handleRadius, 0.40 * contRect.height()
        )
        barRect.moveCenter(contRect.center())
        rounding = barRect.height() / 2

        # the handle will move along this line
        trailLength = contRect.width() - 2 * handleRadius
        xPos = contRect.x() + handleRadius + trailLength * self._handle_position
        iconRad = int(0.7 * handleRadius)
        # center of handle
        icon_x = int(xPos - (1.3 * handleRadius) + (1.3 * iconRad))
        iconRect = QRect(
            QPoint(icon_x, round(barRect.center().y()) - iconRad),
            QSize(2 * iconRad, 2 * iconRad)
        )

        if self.isChecked():
            p.setBrush(self._bar_checked_brush)
            p.drawRoundedRect(barRect, rounding, rounding)
            p.setBrush(self._handle_checked_brush)
            p.drawEllipse(
                QPointF(xPos, barRect.center().y()),
                handleRadius, handleRadius
            )
            icon = qta.icon(self.checked_icon,
                            color=QColor(self.checked_color).darker())
            icon.paint(p, iconRect)

        else:
            p.setBrush(self._bar_brush)
            p.drawRoundedRect(barRect, rounding, rounding)
            p.setPen(self._light_grey_pen)
            p.setBrush(self._handle_brush)
            p.drawEllipse(
                QPointF(xPos, barRect.center().y()),
                handleRadius, handleRadius
            )
            icon = qta.icon(self.unchecked_icon)
            icon.paint(p, iconRect)

        p.end()

    @QtCore.Slot(int)
    def handle_state_change(self, value):
        self._handle_position = 1 if value else 0

    @QtCore.Property(float)
    def handle_position(self):
        return self._handle_position

    @handle_position.setter
    def handle_position(self, pos):
        """change the property
        we need to trigger QWidget.update() method, either by:
            1- calling it here [ what we're doing ].
            2- connecting the QPropertyAnimation.valueChanged() signal to it.
        """
        self._handle_position = pos
        self.update()


def user_string_to_bool(text: str) -> bool:
    """
    Interpret a user's input as a boolean value.

    Strings like "true" should evaluate to True, strings
    like "fa" should evaluate to False, numeric inputs like
    1 or 2 should evaluate to True, numeric inputs like 0 or
    0.0 should evaluate to False, etc.

    Parameters
    ----------
    text : str
        The user's text input as a string. This is usually
        the value directly from a line edit widget.
    """
    if not text:
        return False
    try:
        if text[0].lower() in ('n', 'f', '0'):
            return False
    except (IndexError, AttributeError):
        # Not a string, let's be slightly helpful
        return bool(text)
    return True


def setup_line_edit_data(
    line_edit: QLineEdit,
    value_obj: QDataclassValue,
    from_str: Callable[[str], Any],
    to_str: Callable[[Any], str],
) -> None:
    """
    Setup a line edit for bilateral data exchange with a bridge.

    Parameters
    ----------
    line_edit : QLineEdit
        The line edit to set up.
    value_obj : QDataclassValue
        The bridge member that has the value we care about.
    from_str : callable
        A callable from str to the dataclass value. This is used
        to interpret the contents of the line edit.
    to_str : callable
        A callable from the dataclass value to str. This is used
        to fill the line edit when the dataclass updates.
    """
    def update_dataclass(text: str) -> None:
        try:
            value = from_str(text)
        except ValueError:
            return
        value_obj.put(value)

    def update_widget(value: Any) -> None:
        if not line_edit.hasFocus():
            try:
                text = to_str(value)
            except ValueError:
                return
            line_edit.setText(text)

    starting_value = value_obj.get()
    starting_text = to_str(starting_value)
    line_edit.setText(starting_text)
    line_edit.textEdited.connect(update_dataclass)
    value_obj.changed_value.connect(update_widget)


def describe_comparison_context(attr: str, config: Configuration) -> str:
    """
    Describe in words what value or values we are comparing to.

    Parameters
    ----------
    attr : str
        The attribute, pvname, or other string identifier we are going
        to compare to. This can also be 'shared'.
    config : Configuration
        Typically a DeviceConfiguration, PVConfiguration, or
        ToolConfiguration that has the contextual information for
        understanding attr.
    """
    if not attr:
        return 'Error loading context information'
    if isinstance(config, DeviceConfiguration):
        num_devices = len(config.devices)
        if num_devices == 0:
            return 'Invalid comparison to zero devices'
        if attr == 'shared':
            num_signals = len(config.by_attr)
            if num_signals == 0:
                return 'Invalid comparison to zero signals'
            if num_devices == 1 and num_signals == 1:
                # device_name.signal_name
                return (
                    f'Comparison to value of {config.devices[0]}.'
                    f'{list(config.by_attr)[0]}'
                )
            if num_devices > 1 and num_signals == 1:
                return (
                    f'Comparison to value of {list(config.by_attr)[0]} '
                    f'signal on each of {num_devices} devices'
                )
            if num_devices == 1 and num_signals > 1:
                return (
                    f'Comparison to value of {num_signals} '
                    f'signals on {config.devices[0]}'
                )
            return (
                f'Comparison to value of {num_signals} signals '
                f'on each of {num_devices} devices'
            )
        # Must be one specific signal
        if num_devices == 1:
            # device_name.signal_name
            return f'Comparison to value of {config.devices[0]}.{attr}'
        return (
            f'Comparison to value of {attr} '
            f'on each of {num_devices} devices'
        )
    if isinstance(config, PVConfiguration):
        if attr == 'shared':
            num_pvs = len(config.by_pv)
            if num_pvs == 0:
                return 'Invalid comparison to zero PVs'
            if num_pvs == 1:
                return f'Comparison to value of {list(config.by_pv)[0]}'
            return f'Comparison to value of each of {num_pvs} pvs'
        return f'Comparison to value of {attr}'
    if isinstance(config, ToolConfiguration):
        if isinstance(config.tool, Ping):
            num_hosts = len(config.tool.hosts)
            if num_hosts == 0:
                return 'Invalid comparison to zero ping hosts'
            if attr == 'shared':
                if num_hosts == 1:
                    return (
                        'Comparison to all different results from pinging '
                        f'{config.tool.hosts[0]}'
                    )
                return (
                    'Comparison to all different results from pinging '
                    f'{num_hosts} hosts'
                )
            if num_hosts == 1:
                return (
                    f'Comparison to {attr} result '
                    f'from pinging {config.tool.hosts[0]}'
                )
            return (
                f'Comparison to {attr} result from pinging {num_hosts} hosts'
            )
        return 'Comparison to unknown tool results'
    return 'Invalid comparison'


def describe_step_context(attr: str, step: ProcedureStep) -> str:
    # TODO: actually write this method
    # may not need attr, since ProcedureSteps are flatter
    # Will have to be expanded with each new step type
    return ''


def get_relevant_pvs(
    attr: str,
    config: Configuration
) -> List[Tuple[str, str]]:
    """
    Get the pvs and corresponding attribute name for the provided comparison.

    Parameters
    ----------
    attr : str
        The attribute, pvname or other string identifier to compare to.
        This can also be 'shared'
    config : Configuration
        Typically a DeviceConfiguration, PVConfiguration, or
        ToolConfiguration that has the contextual information for
        understanding attr.
    Returns
    -------
    List[Tuple[str, str]]
        A list of tuples (PV:NAME, device.attr.name) containing the
        relevant pv information
    """
    if isinstance(config, PVConfiguration):
        # we have raw PV's here, with no attrs
        return [(pv, None) for pv in config.by_pv.keys()]
    if isinstance(config, DeviceConfiguration):
        pv_list = []
        if attr == 'shared':
            # Use all pvs in the config
            attrs = config.by_attr.keys()
        else:
            attrs = list([attr])
        for device_name in config.devices:
            dev = util.get_happi_device_by_name(device_name)
            for curr_attr in attrs:
                try:
                    pv = getattr(getattr(dev, curr_attr), 'pvname', None)
                except AttributeError:
                    continue
                if pv:
                    pv_list.append((pv, device_name + '.' + curr_attr))

        return pv_list


def cast_dataclass(data: Any, new_type: Type) -> Any:
    """
    Convert one dataclass to another, keeping values in any same-named fields.

    Parameters
    ----------
    data : Any dataclass instance
        The dataclass instance that we'd like to convert.
    new_type : Any dataclass
        The dataclass type that we'd like to convert.

    Returns
    -------
    casted_data : instance of new_type
        The new dataclass instance.
    """
    data_fields = dataclasses.fields(data)
    new_fields = dataclasses.fields(new_type)
    field_names = set(field.name for field in new_fields)
    new_kwargs = {
        dfield.name: getattr(data, dfield.name) for dfield in data_fields
        if dfield.name in field_names
    }
    return new_type(**new_kwargs)


class MultiInputDialog(QtWidgets.QDialog):
    """
    Generates a dialog widget for requesting an arbitrary number of
    pieces of information.  Selects the input widget type based on the
    initial data type.

    To retrieve the user provided data, call MultiInputDialog.get_info()
    """
    def __init__(
        self,
        *args,
        init_values: Dict[str, Any],
        units: Optional[List[str]] = None,
        **kwargs
    ):
        super().__init__(*args, **kwargs)

        self.init_values = init_values
        self.units = units
        vlayout = QtWidgets.QVBoxLayout(self)
        self.grid_layout = QtWidgets.QGridLayout()
        # add each name and field
        for i, (key, value) in enumerate(init_values.items()):
            spaced_key = key.replace('_', ' ')
            self.grid_layout.addWidget(self.make_label(spaced_key), i, 0)
            self.grid_layout.addWidget(self.make_field(value), i, 1)
            if self.units:
                try:
                    unit_label = QtWidgets.QLabel(self.units[i])
                except IndexError:
                    continue
                self.grid_layout.addWidget(unit_label, i, 2)

        vlayout.addLayout(self.grid_layout)

        # add ok, cancel buttons
        self.button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.ok_button = self.button_box.button(QtWidgets.QDialogButtonBox.Ok)
        self.cancel_button = self.button_box.button(QtWidgets.QDialogButtonBox.Cancel)

        vlayout.addWidget(self.button_box)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

    def make_label(self, key: str) -> QtWidgets.QLabel:
        return QtWidgets.QLabel(key)

    def make_field(self, value: Any) -> QtWidgets.QWidget:
        """
        Make an input field widget for the given value based on its type

        Parameters
        ----------
        value : Any
            The default value to make a input field for

        Returns
        -------
        QtWidgets.QWidget
            The input field widget
        """
        # no newlines allowed
        regexp = QRegularExpression(r'[^\n]*')
        if isinstance(value, str):
            # make text edit
            text_edit = QtWidgets.QLineEdit()
            validator = QRegularExpressionValidator(regexp)
            text_edit.setMaximumHeight(30)
            text_edit.setPlaceholderText(value)
            text_edit.setValidator(validator)
            return text_edit
        elif isinstance(value, int):
            int_edit = QtWidgets.QSpinBox()
            int_edit.setMinimum(-1)
            int_edit.setSpecialValueText('None')
            int_edit.setToolTip('Input -1 to set value to None')
            int_edit.setValue(value)
            return int_edit
        elif isinstance(value, float):
            float_edit = QtWidgets.QDoubleSpinBox()
            float_edit.setMinimum(-1)
            float_edit.setSpecialValueText('None')
            float_edit.setToolTip('Input -1 to set value to None')
            float_edit.setValue(value)
            return float_edit
        else:
            raise RuntimeError(f"Unexpected value {value} of type {type(value).__name__}")

    def get_info(self) -> Dict[str, Any]:
        """
        Collect user provided information.  Returns default values
        provided to the widget at initialization if the user has not
        entered any data.
        """
        info = {}
        for r in range(self.grid_layout.rowCount()):
            key = self.grid_layout.itemAtPosition(r, 0).widget().text()
            input_widget = self.grid_layout.itemAtPosition(r, 1).widget()
            if isinstance(input_widget, QtWidgets.QLineEdit):
                value = input_widget.text()
            elif isinstance(input_widget,
                            (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                value = input_widget.value()

            unspaced_key = key.replace(' ', '_')
            # replace with default value if no input
            info[unspaced_key] = value or self.init_values[unspaced_key]

        return info


def clear_results(config_file: PreparedFile | ProcedureFile) -> None:
    if isinstance(config_file, ProcedureFile):
        # clear all results when making a new run tree
        for step in walk_steps(config_file.root):
            step.step_result = incomplete_result()
            step.verify_result = incomplete_result()
            step.combined_result = incomplete_result()

    elif isinstance(config_file, PreparedFile):
        for comp in config_file.walk_comparisons():
            comp.result = incomplete_result()
        for group in config_file.walk_groups():
            group.result = incomplete_result()


class ConfigTreeModel(QtCore.QAbstractItemModel):
    """
    Item model for tree data.  Goes through all this effort due to the need for
    tooltips, icons, etc.  This model is READ-ONLY, and does not implement
    the ``setData`` method.

    Expects the item to be specifically a TreeItem, which each holds a
    Configuration or Comparison
    """
    def __init__(self, *args, data: TreeItem, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree_data = data or TreeItem()
        self.root_item = self.tree_data
        self.headers = ['Name', 'Status', 'Type']

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int
    ) -> Any:
        """
        Returns the header data for the model.
        Currently only displays horizontal header data

        Parameters
        ----------
        section : int
            section to provide header information for
        orientation : Qt.Orientation
            header orientation, Qt.Horizontal or Qt.Vertical
        role : int
            Qt role to provide header information for

        Returns
        -------
        Any
            requested header data
        """
        if role != Qt.DisplayRole:
            return

        if orientation == Qt.Horizontal:
            return self.headers[section]

    def index(
        self,
        row: int,
        column: int,
        parent: QtCore.QModelIndex = None
    ) -> QtCore.QModelIndex:
        """
        Returns the index of the item in the model.

        In a tree view the rows are defined relative to parent item.  If an
        item is the first child under its parent, it will have row=0,
        regardless of the number of items in the tree.

        Parameters
        ----------
        row : int
            The row of the requested index.
        column : int
            The column of the requested index
        parent : QtCore.QModelIndex, optional
            The parent of the requested index, by default None

        Returns
        -------
        QtCore.QModelIndex
        """
        if not self.hasIndex(row, column, parent):
            return QtCore.QModelIndex()

        parent_item = None
        if not parent or not parent.isValid():
            parent_item = self.root_item
        else:
            parent_item = parent.internalPointer()

        child_item = parent_item.child(row)
        if child_item:
            return self.createIndex(row, column, child_item)

        # all else
        return QtCore.QModelIndex()

    def parent(self, index: QtCore.QModelIndex) -> QtCore.QModelIndex:
        """
        Returns the parent of the given model item.

        Parameters
        ----------
        index : QtCore.QModelIndex
            item to retrieve parent of

        Returns
        -------
        QtCore.QModelIndex
            index of the parent item
        """
        if not index.isValid():
            return QtCore.QModelIndex()
        child = index.internalPointer()
        parent = child.parent()
        if parent == self.root_item:
            return QtCore.QModelIndex()

        return self.createIndex(parent.row(), 0, parent)

    def rowCount(self, parent: QtCore.QModelIndex) -> int:
        """
        Called by tree view to determine number of children an item has.

        Parameters
        ----------
        parent : QtCore.QModelIndex
            index of the parent item being queried

        Returns
        -------
        int
            number of children ``parent`` has
        """
        if not parent.isValid():
            parent_item = self.root_item
        else:
            parent_item = parent.internalPointer()
        return parent_item.childCount()

    def columnCount(self, parent: QtCore.QModelIndex) -> int:
        """
        Called by tree view to determine number of columns of data ``parent`` has

        Parameters
        ----------
        parent : QtCore.QModelIndex

        Returns
        -------
        int
            number of columns ``parent`` has
        """
        if not parent.isValid():
            parent_item = self.root_item
        else:
            parent_item = parent.internalPointer()
        return parent_item.columnCount()

    def data(self, index: QtCore.QModelIndex, role: int) -> Any:
        """
        Returns the data stored under the given ``role`` for the item
        referred to by the ``index``.  Uses and assumes ``TreeItem`` methods.

        Parameters
        ----------
        index : QtCore.QModelIndex
            index that identifies the portion of the model in question
        role : int
            the data role

        Returns
        -------
        Any
            The data to be displayed by the model
        """
        if not index.isValid():
            return None

        item = index.internalPointer()
        # special handling for status info
        if index.column() == 1:
            if role == Qt.ForegroundRole:
                brush = QBrush()
                brush.setColor(item.data(index.column())[1])
                return brush
            if role == Qt.DisplayRole:
                return item.data(1)[0]
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter

        if role == Qt.ToolTipRole:
            return item.tooltip()
        if role == Qt.DisplayRole:
            return item.data(index.column())

        return None


class TreeItem:
    """
    Node in a tree representation of a passive checkout.

    Each node takes a Configuration or Comparison, and provides ``ConfigTreeModel``
    information from it.

    If ``prepared_data`` is provided, Result information can be provided to the
    model via the ``.data()`` method
    """
    result_icon_map = {
        # check mark
        Severity.success: ('\u2713', QColor(0, 128, 0, 255)),
        Severity.warning : ('?', QColor(255, 165, 0, 255)),
        # x mark
        Severity.internal_error: ('\u2718', QColor(255, 0, 0, 255)),
        Severity.error: ('\u2718', QColor(255, 0, 0, 255))
    }

    def __init__(
        self,
        data: Union[Configuration, Comparison],
        prepared_data: Optional[List[PreparedConfiguration, PreparedComparison]] = None
    ) -> None:
        self._data = data
        self.prepared_data = prepared_data
        self.combined_result = None
        self._columncount = 3
        self._children: List[TreeItem] = []
        self._parent = None
        self._row = 0

    def data(self, column: int) -> Any:
        """
        Return the data for the requested column.
        Column 0: name
        Column 1: (status icon, color)
        Column 2: type

        Parameters
        ----------
        column : int
            data column requested

        Returns
        -------
        Any
        """
        if column == 0:
            return self._data.name
        elif column == 1:
            if self.prepared_data:
                prep_results = [d.result for d in self.prepared_data]
                self.combined_result = combine_results(prep_results)
                icon_data = self.result_icon_map[self.combined_result.severity]
                return icon_data
            else:
                return self.result_icon_map[Severity.internal_error]
        elif column == 2:
            return type(self._data).__name__

    def tooltip(self) -> str:
        """ Construct the tooltip based on the stored result """
        if self.combined_result:
            reason = self.combined_result.reason
            return reason.strip('[]').replace(', ', '\n')
        return ''

    def columnCount(self) -> int:
        """ Return the item's column count """
        return self._columncount

    def childCount(self) -> int:
        """ Return the item's child count """
        return len(self._children)

    def child(self, row: int) -> TreeItem:
        """ Return the item's child """
        if row >= 0 and row < self.childCount():
            return self._children[row]

    def parent(self) -> TreeItem:
        """ Return the item's parent """
        return self._parent

    def row(self) -> int:
        """ Return the item's row under its parent """
        return self._row

    def addChild(self, child: TreeItem) -> None:
        """
        Add a child to this item.

        Parameters
        ----------
        child : TreeItem
            Child TreeItem to add to this TreeItem
        """
        child._parent = self
        child._row = len(self._children)
        self._children.append(child)
        self._columncount = max(child.columnCount(), self._columncount)


class AddRowWidget(DesignerDisplay, QWidget):
    """
    A simple row widget with an add button.  To be used when space is precious
    Connect a new-row slot to the add_button signal to create new rows
    """
    filename = 'add_row_widget.ui'

    add_button: QtWidgets.QToolButton
    row_label: QtWidgets.QLabel

    def __init__(self, *args, text='Add new row', **kwargs):
        super().__init__(*args, **kwargs)
        self.add_button.setIcon(qta.icon('ri.add-circle-line'))
        self.row_label.setText(text)


class TableWidgetWithAddRow(QtWidgets.QTableWidget):
    """
    A standard QTableWidget with an AddRowWidget.
    Intended to be a n x 1 table, with each row being a SimpleRowWidget.
    allows drag-and-drop to re-order rows
    Emits table_updated when the table contents change.

    use .add_row() to initialize a new row with an optional dataclass.

    The AddRowWidget is not treated as a row, and as such the following methods
    are modified.
    - rowCount(): Returns super().rowCount() - 1
    - ... and more as I find more methods
    """
    # TODO: try setting up drag-drop functionality at some point.
    add_row_widget: AddRowWidget

    table_updated: ClassVar[QtCore.Signal] = QtCore.Signal()

    def __init__(self, *args, add_row_text: str, title_text: str, row_widget_cls: QtWidgets.QWidget, **kwargs):
        super().__init__(*args, **kwargs)

        # self.dropEvent = self.table_drop_event
        self.setColumnCount(1)
        self.horizontalHeader().setStretchLastSection(True)
        self.setHorizontalHeaderLabels([title_text])
        self.verticalHeader().setHidden(True)
        self.row_widget_cls = row_widget_cls
        self.add_add_row_widget(text=add_row_text)
        self.setSelectionMode(self.NoSelection)

    def add_add_row_widget(self, text: str):
        """ add the AddRowWidget to the end of the specified table-widget"""
        self.add_row_widget = AddRowWidget(text=text)
        self.insertRow(0)
        self.setRowHeight(0, self.add_row_widget.sizeHint().height())
        self.setCellWidget(0, 0, self.add_row_widget)
        self.add_row_widget.add_button.clicked.connect(self.add_row)

    def rowCount(self) -> int:
        # exclude add-row in row counts
        return super().rowCount() - 1

    def add_row(
        self,
        checked: bool = False,
        data: Optional[Any] = None,
        **kwargs
    ) -> None:
        """
        add a new or existing action to the table.

        Parameters
        ----------
        checked : bool, optional
            Unused. Button "clicked" signals often pass this as the first
            positional argument, by default False
        data : Optional[Any], optional
            a Dataclass to initialize the row with, by default None
            used in initializing the table, not in callbacks
        """
        new_row = self.row_widget_cls(data=data)
        # Insert just above the add-row-row
        ins_ind = self.rowCount()
        self.insertRow(ins_ind)
        self.setRowHeight(ins_ind, new_row.sizeHint().height())
        self.setCellWidget(ins_ind, 0, new_row)
        self.setup_delete_button(new_row)
        self.table_updated.emit()

    def setup_delete_button(self, row: QtWidgets.QWidget) -> None:
        """
        Set up the delete button for the specified row.  Assumes `row.delete_button`
        is a QPushButton

        Parameters
        ----------
        row : QtWidgets.QWidget
            A row widget with a QPushButton in the .delete_button field
        """
        # row: SimpleRowWidget, but can't import due to module structure
        delete_icon = self.style().standardIcon(
            QtWidgets.QStyle.SP_TitleBarCloseButton
        )
        row.delete_button.setIcon(delete_icon)

        def inner_delete(*args, **kwargs):
            self.delete_table_row(row)

        row.delete_button.clicked.connect(inner_delete)

    def delete_table_row(self, row: QtWidgets.QWidget) -> None:
        """ slot for a row's delete button.  Removes it from this table. """
        # get the data
        for row_index in range(self.rowCount()):
            widget = self.cellWidget(row_index, 0)
            if widget is row:
                self.removeRow(row_index)
                break

        self.table_updated.emit()


def set_widget_font_size(widget: QWidget, size: int):
    font = widget.font()
    font.setPointSize(size)
    widget.setFont(font)


class EditMode(IntEnum):
    BOOL = 0
    ENUM = 1
    FLOAT = 2
    INT = 3
    STR = 4
    EPICS = 5
    HAPPI = 6


class MultiModeValueEdit(DesignerDisplay, QWidget):
    """
    Widget to edit a single value/dynamic value pair.  This widget contains a
    set of various edit widgets that will be connected to the corresponding
    QDataclassValue instances as appropriate. On first load we will match the
    data type of the saved value (or of the default value). The user will be
    able to pick a different input method via the mode select button and the
    appropriate input widget will be shown.  This is intended to be used to
    edit the "value" and "dynamic_value" attributes of "Comparison" classes and
    of similar constructs. Some of the modes will edit the "dynamic_value" and
    others will edit the plain normal "value".

    Parameters
    ----------
    bridge : QDataclassBridge
        The bridge to the "Comparison" data class.
    value_name : str, optional
        The attribute name of the static value to edit.
        Defaults to "value".
    dynamic_name : str, optional
        The attribute name of the dynamic value to edit.
        Defaults = "value_dynamic".
    ids : QDataclassValue, optional
        The value object that will give us the list of ids (pvnames, devices)
        that are active for this comparison.  This is needed to establish enum
        options.
    devices : QDataclassValue, optional
        The value object that will contain the list of device names if this is
        part of a device config. This is needed to establish enum options. If
        omitted, we'll treat ids as a list of PVs.
    font_pt_size : int, optional
        The size of the font to use for the widget.
    """
    filename = 'multi_mode_value_edit.ui'
    show_tolerance: ClassVar[QSignal] = QSignal(bool)
    refreshed: ClassVar[QSignal] = QSignal()

    # Input widgets
    select_mode_button: QToolButton
    bool_input: QComboBox
    enum_input: QComboBox
    epics_widget: QWidget
    epics_input: QLineEdit
    epics_value_preview: QLabel
    epics_refresh: QToolButton
    happi_widget: QWidget
    happi_select_component: QPushButton
    happi_value_preview: QLabel
    happi_refresh: QToolButton
    float_input: QDoubleSpinBox
    int_input: QSpinBox
    str_input: QLineEdit

    # metadata
    bridge: QDataclassBridge
    value_name: str
    value: QDataclassValue
    dynamic_name: str
    dynamic_value: QDataclassValue
    dynamic_bridge: Optional[QDataclassBridge]
    ids: Optional[QDataclassValue]
    devices: Optional[QDataclassValue]
    happi_select_widget: Optional[HappiDeviceComponentWidget]
    _last_device_name: str
    _is_number: bool
    _prep_dynamic_thread: Optional[ThreadWorker]

    def __init__(
        self,
        bridge: QDataclassBridge,
        value_name: str = 'value',
        dynamic_name: str = 'value_dynamic',
        id_fn: Optional[Callable] = None,
        devices: Optional[list[str]] = None,
        font_pt_size: int = 8,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.bridge = bridge
        self.value_name = value_name
        self.value = getattr(bridge, value_name)
        self.dynamic_name = dynamic_name
        self.dynamic_value = getattr(bridge, dynamic_name)
        self.dynamic_bridge = None
        self.id_fn = id_fn
        self.ids = self.id_fn()
        self.devices = devices
        self.font_pt_size = font_pt_size
        self.happi_select_widget = None
        self._last_device_name = ""
        self._is_number = False
        self._show_tol = False
        self._prep_dynamic_thread = None
        self.setup_widgets()
        self.set_mode_from_data()
        self.setSizePolicy(
            QSizePolicy(
                QSizePolicy.Maximum,
                QSizePolicy.Maximum,
            )
        )

    def setup_widgets(self):
        """
        Connect widgets to edit data classes as appropriate.
        """
        # Data connections and style
        self.bool_input.activated.connect(self.update_from_bool)
        self.enum_input.activated.connect(self.update_from_enum)
        self.epics_input.textEdited.connect(self.update_from_epics)
        self.epics_refresh.clicked.connect(self.update_epics_preview)
        self.setup_refresh_icon(self.epics_refresh)
        self.happi_select_component.clicked.connect(self.select_happi_cpt)
        self.happi_refresh.clicked.connect(self.update_happi_preview)
        self.setup_refresh_icon(self.happi_refresh)
        self.float_input.valueChanged.connect(self.update_from_float)
        self.int_input.valueChanged.connect(self.update_normal)
        self.str_input.textEdited.connect(self.update_normal)

        # Data Validators
        self.epics_input.setValidator(PV_validator)

        for widget in self.children():
            if hasattr(widget, "font"):
                set_widget_font_size(widget, self.font_pt_size)

        # Hide bool/str if "Number" annotation.
        for field in fields(self.bridge.data):
            if field.name == self.value_name:
                if field.type in (
                    Number,
                    "Number",
                    Optional[Number],
                    "Optional[Number]",
                ):
                    self._is_number = True
                break

        # Select mode
        menu = QMenu()
        if not self._is_number:
            use_bool = menu.addAction("&Bool")
            use_bool.triggered.connect(partial(self.set_mode, EditMode.BOOL)),
            use_enum = menu.addAction("&Enum")
            use_enum.triggered.connect(partial(self.set_mode, EditMode.ENUM))
        use_float = menu.addAction("&Float")
        use_float.triggered.connect(partial(self.set_mode, EditMode.FLOAT))
        use_int = menu.addAction("&Int")
        use_int.triggered.connect(partial(self.set_mode, EditMode.INT))
        if not self._is_number:
            use_str = menu.addAction("&String")
            use_str.triggered.connect(partial(self.set_mode, EditMode.STR))
        use_epics = menu.addAction("EPI&CS")
        use_epics.triggered.connect(partial(self.set_mode, EditMode.EPICS))
        use_happi = menu.addAction("&Happi")
        use_happi.triggered.connect(partial(self.set_mode, EditMode.HAPPI))
        self.select_mode_button.setMenu(menu)
        self.select_mode_button.setPopupMode(
            self.select_mode_button.InstantPopup
        )

    def setup_refresh_icon(self, button: QToolButton):
        """
        Assign the refresh icon to a QToolButton.
        """
        icon = self.style().standardIcon(QStyle.SP_BrowserReload)
        button.setIcon(icon)

    def update_from_bool(self, index: int) -> None:
        """
        When the bool widget is updated by the user, save a boolean.
        """
        self.value.put(bool(index))

    def update_from_enum(self, index: int) -> None:
        """
        When the enum widget is updated by the user, save a string.
        """
        text = self.enum_input.itemText(index)
        self.value.put(text)

    def update_from_float(self, value: float) -> None:
        """
        When the float widget is updated by the user, save a float.
        """
        self.value.put(float(value))

    def update_normal(self, value: Any) -> None:
        """
        Catch-all for updates that are already correct.
        These are cases where no preprocessing of value is needed.
        """
        match_line_edit_text_width(self.str_input, text=str(value),
                                   minimum=50, buffer=10)
        self.value.put(value)

    def update_from_epics(self, text: str) -> None:
        """
        When the EPICS widget is updated by the user, save the PV name.
        """
        match_line_edit_text_width(self.epics_input, text=text, minimum=50, buffer=10)
        self.epics_input.setToolTip(text)
        self.dynamic_bridge.pvname.put(text.strip())

    def update_epics_preview(self) -> None:
        """
        When the user asks for a new value, get a value from EPICS.
        """
        # Prepare each time to get updated value
        def _prepare_value():
            value = self.dynamic_value.get()
            asyncio.run(value.prepare(DataCache()))
            self.epics_value_preview.setText(str(value.get()))
            if isinstance(value.get(), (float, int)):
                self._show_tol = True
            else:
                self._show_tol = False
            self.show_tolerance.emit(self._show_tol)
            self.refreshed.emit()

        def _handle_errors(ex: Exception):
            if isinstance(ex, DynamicValueError):
                QtWidgets.QMessageBox.warning(
                    self,
                    'Failed to connect to PV',
                    'Unable to gather PV information for preview. '
                    'PV may not exist or be inaccessible',
                )
            else:
                raise ex

        if self._prep_dynamic_thread:
            if self._prep_dynamic_thread.isRunning():
                # TODO: Consider threadpools for this and other threading apps?
                for i in range(10):
                    QTimer.singleShot(1, self.update_epics_preview)

        self._prep_dynamic_thread = ThreadWorker(_prepare_value)
        self._prep_dynamic_thread.error_raised.connect(_handle_errors)
        self._prep_dynamic_thread.start()

    def select_happi_cpt(self) -> None:
        """
        When the user clicks on the happi device name, open the cpt chooser.
        Unlike other uses of this GUI, this one is used to select both the
        device and component all at once, since we can only have one
        target for the dynamic value.
        """
        if self.happi_select_widget is None:
            widget = HappiDeviceComponentWidget(
                client=util.get_happi_client()
            )
            widget.item_search_widget.happi_items_selected.connect(
                self.new_happi_devices
            )
            widget.device_widget.attributes_selected.connect(
                self.new_happi_attrs
            )
            self.happi_select_widget = widget
        self.happi_select_widget.show()
        self.happi_select_widget.activateWindow()

        try:
            current_device = self.dynamic_value.get().device_name
        except AttributeError:
            return
        if current_device:
            self.happi_select_widget.item_search_widget.edit_filter.setText(
                current_device
            )

    def new_happi_devices(self, device_names: List[str]) -> None:
        """
        Cache the name of the last device that was selected.
        The selection widget gives us a list, but we can only accept
        one item, so the first element is selected.
        """
        if device_names:
            self._last_device_name = device_names[0]

    def new_happi_attrs(self, attr_names: List[OphydAttributeData]) -> None:
        """
        Set the new happi device/attr on the dataclass and on the display.
        This takes the selection we just chose in the UI and also the
        cached device name.
        The selection widget gives us a list, but we can only accept
        one item, so the first element is selected.
        """
        if attr_names:
            self.dynamic_bridge.device_name.put(self._last_device_name)
            self.dynamic_bridge.signal_attr.put(attr_names[0].attr)
            self.update_happi_text()

    def update_happi_text(self) -> None:
        """
        Update the text on the happi selection button as appropriate.
        """
        happi_value = self.dynamic_value.get()
        if happi_value is not None:
            if not happi_value.device_name or not happi_value.signal_attr:
                text = "click to select"
            else:
                text = f"{happi_value.device_name}.{happi_value.signal_attr}"
            self.happi_select_component.setText(text)
            self.happi_select_component.setToolTip(text)

    def update_happi_preview(self) -> None:
        """
        When the user asks for a new value, query happi and make a device.
        """
        def _prepare_value():
            value = self.dynamic_value.get()
            asyncio.run(value.prepare(DataCache()))
            self.happi_value_preview.setText(str(value.get()))
            if isinstance(value.get(), (float, int)):
                self._show_tol = True
            else:
                self._show_tol = False

            self.show_tolerance.emit(self._show_tol)
            self.refreshed.emit()

        def _handle_errors(ex: Exception):
            if isinstance(ex, DynamicValueError):
                QtWidgets.QMessageBox.warning(
                    self,
                    'Failed to connect to device',
                    'Unable to gather information from happi device for preview. '
                    'Device might be unset or failed to connect',
                )
            else:
                raise ex

        if self._prep_dynamic_thread:
            if self._prep_dynamic_thread.isRunning():
                # TODO: Consider threadpools for this and other threading apps?
                for i in range(10):
                    QTimer.singleShot(1, self.update_happi_preview)

        self._prep_dynamic_thread = ThreadWorker(_prepare_value)
        self._prep_dynamic_thread.error_raised.connect(_handle_errors)
        self._prep_dynamic_thread.start()

    def set_mode_from_data(self) -> None:
        """
        Set the expected mode from the current data.
        """
        mode = None
        dynamic = self.dynamic_value.get()  # get from QDataclassBridge
        if dynamic is not None:
            if isinstance(dynamic, EpicsValue):
                mode = EditMode.EPICS
            elif isinstance(dynamic, HappiValue):
                mode = EditMode.HAPPI
            else:
                raise TypeError(
                    f"Unexpected dynamic value {dynamic}."
                )

            # prepare dynamic value
            def prep_dynamic_value() -> Any:
                try:
                    asyncio.run(dynamic.prepare(DataCache()))
                except DynamicValueError as ex:
                    logger.warning('Unable to prepare dynamic value during '
                                   f'input widget initialization: {ex}')
                    self.set_mode(EditMode.STR)
                    return
                self.set_mode(mode)

            self.prep_dynamic_thread = ThreadWorker(prep_dynamic_value)
            self.prep_dynamic_thread.start()
        else:
            static = self.value.get()
            if isinstance(static, bool):
                mode = EditMode.BOOL
            elif isinstance(static, float):
                mode = EditMode.FLOAT
            elif isinstance(static, int):
                mode = EditMode.INT
            elif isinstance(static, str):
                self.setup_enums(set_mode=True)
                return
            elif static is None:
                if self._is_number:
                    mode = EditMode.INT
                else:
                    mode = EditMode.STR
            else:
                raise TypeError(
                    f"Unexpected static value {static}"
                )

            self.set_mode(mode)

    def setup_enums(self, set_mode: bool = False) -> None:
        """
        Get enum strings and populate enum combo
        if enums are found, sets the mode to enum
        """
        self.enum_input.clear()

        self.ids = self.id_fn()
        if self.ids is None:
            # no identifiers... nothing to do, but this shouldn't happen
            return
        if self.devices is None:
            # Collect signals from ids as pv names
            # self.ids: List[str]
            signal_cache = get_signal_cache()
            sigs: List[EpicsSignalRO] = []
            for id in self.ids:
                sigs.append(signal_cache[id])

        else:
            # Collect signals from ids as device attrs
            # self.ids: List[Tuple[str, str]] (device, attr)
            device_names = self.devices
            devices = []
            for device_name in device_names:
                try:
                    devices.append(util.get_happi_device_by_name(device_name))
                except MissingHappiDeviceError as ex:
                    logger.debug(f'Device missing in enum value setup: {ex}')
                    continue
            sigs: List[EpicsSignal] = []
            for dev, attr in self.ids:
                for device in devices:
                    try:
                        sig = getattr(device, attr)
                    except AttributeError:
                        continue
                    else:
                        sigs.append(sig)

        enums_in_order = []

        def get_signal_enums():
            start = time.monotonic()
            for sig in sigs:
                try:
                    sig.wait_for_connection(timeout=1)
                except TimeoutError:
                    pass
                if time.monotonic() - start >= 1:
                    break

            enum_set = set()
            for sig in sigs:
                if sig.enum_strs is not None:
                    for enum_str in sig.enum_strs:
                        if enum_str not in enum_set:
                            enum_set.add(enum_str)
                            enums_in_order.append(enum_str)

        def fill_enums():
            for text in enums_in_order:
                self.enum_input.addItem(text)
            value = str(self.value.get())
            if value in enums_in_order:
                self.enum_input.setCurrentText(value)

            if set_mode:
                if enums_in_order:
                    self.set_mode(EditMode.ENUM)
                else:
                    self.set_mode(EditMode.STR)

        self.thread_worker = BusyCursorThread(func=get_signal_enums)
        self.thread_worker.task_finished.connect(fill_enums)
        self.thread_worker.start()

    def set_mode(self, mode: EditMode) -> None:
        """
        Change the mode of the edit widget.
        This adjusts the dynamic data classes as needed and
        shows only the correct edit widget.
        """
        # Hide all the widgets
        self.epics_widget.hide()
        self.happi_widget.hide()
        self.bool_input.hide()
        self.enum_input.hide()
        self.float_input.hide()
        self.int_input.hide()
        self.str_input.hide()
        if mode == EditMode.EPICS:
            if not isinstance(self.dynamic_value.get(), EpicsValue):
                self.dynamic_value.put(EpicsValue(pvname=""))
            self.dynamic_bridge = QDataclassBridge(self.dynamic_value.get())
            self.epics_input.setText(self.dynamic_bridge.pvname.get())
            self.epics_widget.show()
        elif mode == EditMode.HAPPI:
            if not isinstance(self.dynamic_value.get(), HappiValue):
                self.dynamic_value.put(
                    HappiValue(device_name="", signal_attr="")
                )
            self.dynamic_bridge = QDataclassBridge(self.dynamic_value.get())
            self.update_happi_text()
            self.happi_widget.show()
        else:
            self.dynamic_value.put(None)
            self.dynamic_bridge = None
        if mode == EditMode.BOOL:
            self.bool_input.setCurrentIndex(int(bool(self.value.get())))
            self._show_tol = False
            self.bool_input.show()
        elif mode == EditMode.ENUM:
            self.setup_enums()
            self._show_tol = False
            self.enum_input.show()
        elif mode == EditMode.FLOAT:
            try:
                value = float(self.value.get())
            except (ValueError, TypeError):
                value = 0.0
            self._show_tol = True
            self.float_input.setValue(value)
            self.float_input.show()
        elif mode == EditMode.INT:
            try:
                value = int(self.value.get())
            except (ValueError, TypeError):
                value = 0
            self._show_tol = True
            self.int_input.setValue(value)
            self.int_input.show()
        elif mode == EditMode.STR:
            self._show_tol = False
            self.str_input.setText(str(self.value.get()))
            self.str_input.show()

        self.select_mode_button.setToolTip(
            f"Current mode: {mode.name}"
        )
        self.show_tolerance.emit(self._show_tol)


def disable_widget(widget: QWidget) -> QWidget:
    """ Disable widget, recurse through layouts """
    # TODO: revisit, is there a better way to do this?
    for idx in range(widget.layout().count()):
        layout_item = widget.layout().itemAt(idx)
        if isinstance(layout_item, QLayout):
            disable_widget(layout_item)
        else:
            wid = layout_item.widget()
            if wid:
                wid.setEnabled(False)
    return widget


def gather_relevant_identifiers(
    comp: Comparison,
    group: Union[DeviceConfiguration, PVConfiguration, ToolConfiguration, SetValueStep]
) -> list[str]:
    """
    Gathers identifiers for ``comp`` from its parent ``group``.  ``comp`` must
    be present in ``group``, else an empty list will be returned

    Identifiers are typically device+attribute pairs, or raw EPICS PVs

    This function will need to be updated when new configurations or steps are added

    Parameters
    ----------
    comp : Comparison
        the comparison in question
    group : Union[DeviceConfiguration, PVConfiguration]
        a configuration holding ``comp``

    Returns
    -------
    list[str]
        the identifiers related to ``comp``, or an empty list if none are found
    """
    identifiers = []
    if isinstance(group, DeviceConfiguration):
        for device in group.devices:
            for attr, comparisons in group.by_attr.items():
                for comparison in comparisons + group.shared:
                    if comparison == comp:
                        identifiers.append((device, attr))
    elif isinstance(group, PVConfiguration):
        for pvname, comparisons in group.by_pv.items():
            for comparison in comparisons + group.shared:
                if comparison == comp:
                    identifiers.append(pvname)
    elif isinstance(group, ToolConfiguration):
        for result_key, comparisons in group.by_attr.items():
            for comparison in comparisons + group.shared:
                if comparison == comp:
                    identifiers.append(result_key)
    elif isinstance(group, SetValueStep):
        for check in group.success_criteria:
            if check.comparison == comp:
                signal = check.to_signal()
                if signal:
                    identifiers.append(signal.pvname)

    return identifiers
