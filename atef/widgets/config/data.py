"""
Widgets used for manipulating the configuration data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, List, Optional, Protocol
from weakref import WeakValueDictionary

from qtpy.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QLayout,
                            QLineEdit, QMessageBox, QPlainTextEdit,
                            QPushButton, QSpinBox, QStyle, QTableWidget,
                            QTableWidgetItem, QToolButton, QVBoxLayout,
                            QWidget)

from atef.check import Comparison
from atef.config import (Configuration, ConfigurationGroup,
                         DeviceConfiguration, GroupResultMode, PVConfiguration)
from atef.enums import Severity
from atef.qt_helpers import QDataclassBridge, QDataclassList
from atef.reduce import ReduceMethod
from atef.tools import Ping
from atef.type_hints import PrimitiveType
from atef.widgets.core import DesignerDisplay
from atef.widgets.utils import FrameOnEditFilter, match_line_edit_text_width

from .utils import (BulkListWidget, ComponentListWidget, DeviceListWidget,
                    setup_line_edit_data, user_string_to_bool)


class AnyDataclass(Protocol):
    """
    Protocol stub shamelessly lifted from stackoverflow to hint at dataclass
    """
    __dataclass_fields__: Dict


class DataWidget(QWidget):
    """
    Base class for widgets that manipulate dataclasses.

    Defines the init args for all data widgets and handles synchronization
    of the ``QDataclassBridge`` instances. This is done so that only data
    widgets need to consider how to handle bridges and the page classes
    simply need to pass in data structures, rather than needing to keep track
    of how two widgets editing the same data structure must share the same
    bridge object.

    Parameters
    ----------
    data : any dataclass
        The dataclass that the widget needs to manipulate. Most widgets are
        expecting either specific dataclasses or dataclasses that have
        specific matching fields.
    kwargs : QWidget kwargs
        Passed directly to QWidget's __init__. Likely unused in most cases.
        Even parent is unlikely to see use because parent is set automatically
        when a widget is inserted into a layout.
    """
    _bridge_cache: ClassVar[
        WeakValueDictionary[int, QDataclassBridge]
    ] = WeakValueDictionary()
    bridge: QDataclassBridge
    data: AnyDataclass

    def __init__(self, data: AnyDataclass, **kwargs):
        super().__init__(**kwargs)
        self.data = data
        try:
            # TODO figure out better way to cache these
            # TODO worried about strange deallocation timing race conditions
            self.bridge = self._bridge_cache[id(data)]
        except KeyError:
            bridge = QDataclassBridge(data)
            self._bridge_cache[id(data)] = bridge
            self.bridge = bridge


class NameMixin:
    """
    Mixin class for distributing init_name
    """
    def init_name(self):
        """
        Set up the name_edit widget appropriately.
        """
        # Load starting text
        load_name = self.bridge.name.get() or ''
        self.last_name = load_name
        self.name_edit.setText(load_name)
        # Set up the saving/loading
        self.name_edit.textEdited.connect(self.update_saved_name)
        self.bridge.name.changed_value.connect(self.apply_new_name)

    def update_saved_name(self, name: str):
        """
        When the user edits the name, write to the config.
        """
        self.last_name = self.name_edit.text()
        self.bridge.name.put(name)

    def apply_new_name(self, text: str):
        """
        If the text changed in the data, update the widget.

        Only run if needed to avoid annoyance with cursor repositioning.
        """
        if text != self.last_name:
            self.name_edit.setText(text)


class NameDescTagsWidget(DesignerDisplay, NameMixin, DataWidget):
    """
    Widget for displaying and editing the name, description, and tags fields.

    Any of these will be automatically disabled if the data source is missing
    the corresponding field.

    As a convenience, this widget also holds a parent_button in a convenient
    place for page layouts, since it is expected that this will be near the
    top of the page, and an "extra_text_label" QLabel for general use.
    """
    filename = 'name_desc_tags_widget.ui'

    name_edit: QLineEdit
    name_frame: QFrame
    desc_edit: QPlainTextEdit
    desc_frame: QFrame
    tags_content: QVBoxLayout
    add_tag_button: QToolButton
    tags_frame: QFrame
    parent_button: QToolButton
    extra_text_label: QLabel

    last_name: str
    last_desc: str

    def __init__(self, data: AnyDataclass, **kwargs):
        super().__init__(data=data, **kwargs)
        try:
            self.bridge.name
        except AttributeError:
            self.name_frame.hide()
        else:
            self.init_name()
        try:
            self.bridge.description
        except AttributeError:
            self.desc_frame.hide()
        else:
            self.init_desc()
        try:
            self.bridge.tags
        except AttributeError:
            self.tags_frame.hide()
        else:
            self.init_tags()

    def init_desc(self):
        """
        Set up the desc_edit widget appropriately.
        """
        # Load starting text
        load_desc = self.bridge.description.get() or ''
        self.last_desc = load_desc
        self.desc_edit.setPlainText(load_desc)
        # Setup the saving/loading
        self.desc_edit.textChanged.connect(self.update_saved_desc)
        self.bridge.description.changed_value.connect(self.apply_new_desc)
        self.update_text_height()
        self.desc_edit.textChanged.connect(self.update_text_height)

    def update_saved_desc(self):
        """
        When the user edits the desc, write to the config.
        """
        self.last_desc = self.desc_edit.toPlainText()
        self.bridge.description.put(self.last_desc)

    def apply_new_desc(self, desc: str):
        """
        When some other widget updates the description, update it here.
        """
        if desc != self.last_desc:
            self.desc_edit.setPlainText(desc)

    def update_text_height(self):
        """
        When the user edits the desc, make the text box the correct height.
        """
        line_count = max(self.desc_edit.document().size().toSize().height(), 1)
        self.desc_edit.setFixedHeight(line_count * 13 + 12)

    def init_tags(self):
        """
        Set up the various tags widgets appropriately.
        """
        tags_list = TagsWidget(
            data_list=self.bridge.tags,
            layout=QHBoxLayout(),
        )
        self.tags_content.addWidget(tags_list)

        def add_tag():
            if tags_list.widgets and not tags_list.widgets[-1].line_edit.text().strip():
                # Don't add another tag if we haven't filled out the last one
                return

            elem = tags_list.add_item('')
            elem.line_edit.setFocus()

        self.add_tag_button.clicked.connect(add_tag)


class TagsWidget(QWidget):
    """
    A widget used to edit a QDataclassList tags field.

    Aims to emulate the look and feel of typical tags fields
    in online applications.

    Parameters
    ----------
    data_list : QDataclassList
        The dataclass list to edit using this widget.
    layout : QLayout
        The layout to use to arrange our labels. This should be an
        instantiated but not placed layout. This lets us have some
        flexibility in whether we arrange things horizontally,
        vertically, etc.
    """
    widgets: List[TagsElem]

    def __init__(
        self,
        data_list: QDataclassList,
        layout: QLayout,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.data_list = data_list
        self.setLayout(layout)
        self.widgets = []
        starting_list = data_list.get()
        if starting_list is not None:
            for starting_value in starting_list:
                self.add_item(starting_value, init=True)

    def add_item(
        self,
        starting_value: str,
        init: bool = False,
        **kwargs,
    ) -> TagsElem:
        """
        Create and add new editable widget element to this widget's layout.

        This can either be an existing string on the dataclass list to keep
        track of, or it can be used to add a new string to the dataclass list.

        This method will also set up the signals and slots for the new widget.

        Parameters
        ----------
        starting_value : str
            The starting text value for the new widget element.
            This should match the text exactly for tracking existing
            strings.
        checked : bool, optional
            This argument is unused, but it will be sent by various button
            widgets via the "clicked" signal so it must be present.
        init : bool, optional
            Whether or not this is the initial initialization of this widget.
            This will be set to True in __init__ so that we don't mutate
            the underlying dataclass. False, the default, means that we're
            adding a new string to the dataclass, which means we should
            definitely append it.
        **kwargs : from qt signals
            Other kwargs sent along with qt signals will be ignored.

        Returns
        -------
        strlistelem : StrListElem
            The widget created by this function call.
        """
        new_widget = TagsElem(starting_value, self)
        self.widgets.append(new_widget)
        if not init:
            self.data_list.append(starting_value)
        self.layout().addWidget(new_widget)
        return new_widget

    def save_item_update(self, item: TagsElem, new_value: str) -> None:
        """
        Update the dataclass as appropriate when the user submits a new value.

        Parameters
        ----------
        item : StrListElem
            The widget that the user has edited.
        new_value : str
            The value that the user has submitted.
        """
        index = self.widgets.index(item)
        self.data_list.put_to_index(index, new_value)

    def remove_item(self, item: TagsElem) -> None:
        """
        Update the dataclass as appropriate when the user removes a value.

        Parameters
        ----------
        item : StrListElem
            The widget that the user has clicked the delete button for.
        """
        index = self.widgets.index(item)
        self.widgets.remove(item)
        self.data_list.remove_index(index)
        item.deleteLater()


class TagsElem(DesignerDisplay, QWidget):
    """
    A single element for the TagsWidget.

    Has a QLineEdit for changing the text and a delete button.
    Changes its style to no frame when it has text and is out of focus.
    Only shows the delete button when the text is empty.

    Parameters
    ----------
    start_text : str
        The starting text for this tag.
    tags_widget : TagsWidget
        A reference to the TagsWidget that contains this widget.
    """
    filename = 'tags_elem.ui'

    line_edit: QLineEdit
    del_button: QToolButton

    def __init__(self, start_text: str, tags_widget: TagsWidget, **kwargs):
        super().__init__(**kwargs)
        self.line_edit.setText(start_text)
        self.tags_widget = tags_widget
        edit_filter = FrameOnEditFilter(parent=self)
        edit_filter.set_no_edit_style(self.line_edit)
        self.line_edit.installEventFilter(edit_filter)
        self.on_text_changed(start_text)
        self.line_edit.textChanged.connect(self.on_text_changed)
        self.line_edit.textEdited.connect(self.on_text_edited)
        self.del_button.clicked.connect(self.on_del_clicked)
        icon = self.style().standardIcon(QStyle.SP_TitleBarCloseButton)
        self.del_button.setIcon(icon)

    def on_text_changed(self, text: str) -> None:
        """
        Edit our various visual elements when the text changes.

        This will do all of the following:
        - make the delete button show only when the text field is empty
        - adjust the size of the text field to be roughly the size of the
          string we've inputted
        """
        # Show or hide the del button as needed
        self.del_button.setVisible(not text)
        # Adjust the width to match the text
        match_line_edit_text_width(self.line_edit, text=text)

    def on_data_changed(self, data: str) -> None:
        """
        Change the text displayed here using new data, if needed.
        """
        if self.line_edit.text() != data:
            self.line_edit.setText(data)

    def on_text_edited(self, text: str) -> None:
        """
        Update the dataclass when the user edits the text.
        """
        self.tags_widget.save_item_update(
            item=self,
            new_value=text,
        )

    def on_del_clicked(self, **kwargs) -> None:
        """
        Tell the QTagsWidget when our delete button is clicked.
        """
        self.tags_widget.remove_item(self)


class ConfigurationGroupWidget(DesignerDisplay, DataWidget):
    """
    Widget for modifying most unique fields in ConfigurationGroup.

    The fields handled here are:

    - values: dict[str, Any]
    - mode: GroupResultMode

    The configs field will be modified by the ConfigurationGroupRowWidget,
    which is intended to be used many times, once each to handle each
    sub-Configuration instance.
    """
    filename = 'configuration_group_widget.ui'

    values_label: QLabel
    values_table: QTableWidget
    add_value_button: QPushButton
    del_value_button: QPushButton
    mode_combo: QComboBox

    adding_new_row: bool

    def __init__(self, data: ConfigurationGroup, **kwargs):
        super().__init__(data=data, **kwargs)
        # Fill the mode combobox and keep track of the index mapping
        self.mode_indices = {}
        self.modes = []
        for index, result in enumerate(GroupResultMode):
            self.mode_combo.addItem(result.value)
            self.mode_indices[result] = index
            self.modes.append(result)
        # Set up the bridge -> combo and combo -> bridge signals
        self.bridge.mode.changed_value.connect(self.update_mode_combo)
        self.mode_combo.activated.connect(self.update_mode_bridge)
        # Set the initial combobox state
        self.update_mode_combo(self.bridge.mode.get())
        self.add_value_button.clicked.connect(self.add_value_to_table)
        self.adding_new_row = False
        for name, value in self.bridge.values.get().items():
            self.add_value_to_table(name=name, value=value, startup=True)
        self.on_table_edit(0, 0)
        self.resize_table()
        self.values_table.cellChanged.connect(self.on_table_edit)
        self.del_value_button.clicked.connect(self.delete_selected_rows)

    def update_mode_combo(self, mode: GroupResultMode, **kwargs):
        """
        Take a mode from the bridge and use it to update the combobox.
        """
        self.mode_combo.setCurrentIndex(self.mode_indices[mode])

    def update_mode_bridge(self, index: int, **kwargs):
        """
        Take a user's combobox selection and use it to update the bridge.
        """
        self.bridge.mode.put(self.modes[index])

    def add_value_to_table(
        self,
        checked: bool = False,
        name: Optional[str] = None,
        value: Any = None,
        startup: bool = False,
        **kwargs,
    ):
        self.adding_new_row = True
        self.values_label.show()
        self.values_table.show()
        new_row = self.values_table.rowCount()
        self.values_table.insertRow(new_row)
        name_item = QTableWidgetItem()
        name = name if name is not None else ''
        value = value if value is not None else ''
        name_item.setText(name)
        value_item = QTableWidgetItem()
        value_item.setText(value)
        type_readback_widget = QLabel()
        self.values_table.setItem(new_row, 0, name_item)
        self.values_table.setItem(new_row, 1, value_item)
        self.values_table.setCellWidget(new_row, 2, type_readback_widget)
        self.resize_table()
        self.adding_new_row = False
        if not startup:
            self.on_table_edit(new_row, 0)

    def resize_table(self):
        row_count = self.values_table.rowCount()
        # Hide when the table is empty
        if row_count:
            self.values_label.show()
            self.values_table.show()
            self.del_value_button.show()
        else:
            self.values_label.hide()
            self.values_table.hide()
            self.del_value_button.hide()
            return
        # Resize the table, should fit up to 3 rows
        per_row = 30
        height = min((row_count + 1) * per_row, 4 * per_row)
        self.values_table.setFixedHeight(height)

    def on_table_edit(self, row: int, column: int):
        if self.adding_new_row:
            return
        data = []
        for row_index in range(self.values_table.rowCount()):
            name = self.values_table.item(row_index, 0).text()
            value_text = self.values_table.item(row_index, 1).text()
            type_label = self.values_table.cellWidget(row_index, 2)
            try:
                value = float(value_text)
            except (ValueError, TypeError):
                # Not numeric
                value = value_text
                type_label.setText('str')
            else:
                # Numeric, but could be int or float
                if '.' in value_text:
                    type_label.setText('float')
                else:
                    try:
                        value = int(value_text)
                    except (ValueError, TypeError):
                        # Something like 1e-4
                        type_label.setText('float')
                    else:
                        # Something like 3
                        type_label.setText('int')
            data.append((name, value))
        data_dict = {}
        for name, value in sorted(data):
            data_dict[name] = value
        self.bridge.values.put(data_dict)

    def delete_selected_rows(self, *args, **kwargs):
        selected_rows = set()
        for item in self.values_table.selectedItems():
            selected_rows.add(item.row())
        if not selected_rows:
            return
        reply = QMessageBox.question(
            self,
            'Confirm deletion',
            (
                'Are you sure you want to delete '
                f'these {len(selected_rows)} rows?'
            ),
        )
        if reply != QMessageBox.Yes:
            return
        for row in reversed(sorted(selected_rows)):
            self.values_table.removeRow(row)
        self.on_table_edit(0, 0)
        self.resize_table()


class DeviceConfigurationWidget(DesignerDisplay, DataWidget):
    """
    Handle the unique static fields from DeviceConfiguration.

    The fields handled fully here are:

    - devices: List[str]

    The fields handled partially here are:

    - by_attr: Dict[str, List[Comparison]]
    - shared: List[Comparison] = field(default_factory=list)

    This will only put empty lists into the by_attr dict.
    Filling those lists will be the responsibility of the
    DeviceConfigurationPageWidget.

    The shared list will be used a place to put configurations
    that have had their attr deleted instead of just dropping
    those entirely, but adding to the shared list will normally
    be the repsonsibility of the page too.
    """
    filename = 'device_configuration_widget.ui'

    devices_layout: QVBoxLayout
    signals_layout: QVBoxLayout
    # Link up to previous implementation of ComponentListWidget
    component_name_list: QDataclassList

    def __init__(self, data: DeviceConfiguration, **kwargs):
        super().__init__(data=data, **kwargs)
        self.device_widget = DeviceListWidget(
            data_list=self.bridge.devices
        )
        list_holder = ListHolder(
            some_list=list(self.bridge.by_attr.get()),
        )
        self.component_name_list = QDataclassList.of_type(str)(
            data=list_holder,
            attr='some_list',
            parent=self,
        )
        self.component_name_list.added_value.connect(self.add_new_signal)
        self.component_name_list.removed_value.connect(self.remove_signal)
        self.cpt_widget = ComponentListWidget(
            data_list=self.component_name_list,
            get_device_list=self.get_device_list,
        )
        self.devices_layout.addWidget(self.device_widget)
        self.signals_layout.addWidget(self.cpt_widget)

    def get_device_list(self) -> List[str]:
        return self.bridge.devices.get()

    def add_new_signal(self, name: str):
        comparisons_dict = self.bridge.by_attr.get()
        if name not in comparisons_dict:
            comparisons_dict[name] = []
            self.bridge.by_attr.updated.emit()

    def remove_signal(self, name: str):
        comparisons_dict = self.bridge.by_attr.get()
        try:
            old_comparisons = comparisons_dict[name]
        except KeyError:
            # Nothing to do, there was nothing here
            pass
        else:
            # Don't delete the comparisons, migrate to "shared" instead
            for comparison in old_comparisons:
                self.bridge.shared.append(comparison)
            self.bridge.shared.updated.emit()
            del comparisons_dict[name]
            self.bridge.by_attr.updated.emit()


@dataclass
class ListHolder:
    """Dummy dataclass to match ComponentListWidget API"""
    some_list: List


class PVConfigurationWidget(DataWidget):
    """
    Handle the unique static fields from PVConfiguration.

    The fields handled partially here are:

    - by_pv: Dict[str, List[Comparison]]
    - shared: List[Comparison] = field(default_factory=list)

    This will only put empty lists into the by_pv dict.
    Filling those lists will be the responsibility of the
    PVConfigurationPageWidget.

    The shared list will be used a place to put configurations
    that have had their pv deleted instead of just dropping
    those entirely, but adding to the shared list will normally
    be the repsonsibility of the page too.
    """
    # This is not a DesignerDisplay, it's just an augmented BulkListWidget
    filename = None

    pv_selector: BulkListWidget
    # Link up to previous implementation of BulkListWidget
    pvname_list: QDataclassList

    def __init__(self, data: PVConfiguration, **kwargs):
        super().__init__(data=data, **kwargs)
        list_holder = ListHolder(
            some_list=list(self.bridge.by_pv.get()),
        )
        self.pvname_list = QDataclassList.of_type(str)(
            data=list_holder,
            attr='some_list',
            parent=self,
        )
        self.pvname_list.added_value.connect(self.add_new_signal)
        self.pvname_list.removed_value.connect(self.remove_signal)
        self.pv_selector = BulkListWidget(
            data_list=self.pvname_list,
        )
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)
        self.layout().addWidget(self.pv_selector)

    def add_new_signal(self, name: str):
        comparisons_dict = self.bridge.by_pv.get()
        if name not in comparisons_dict:
            comparisons_dict[name] = []
            self.bridge.by_pv.updated.emit()

    def remove_signal(self, name: str):
        comparisons_dict = self.bridge.by_pv.get()
        try:
            old_comparisons = comparisons_dict[name]
        except KeyError:
            # Nothing to do, there was nothing here
            pass
        else:
            # Don't delete the comparisons, migrate to "shared" instead
            for comparison in old_comparisons:
                self.bridge.shared.append(comparison)
            self.bridge.shared.updated.emit()
            del comparisons_dict[name]
            self.bridge.by_pv.updated.emit()


class PingWidget(DesignerDisplay, DataWidget):
    """
    Widget that modifies the fields in the Ping tool.

    These fields are:
    - hosts: List[str] = field(default_factory=list)
    - count: int = 3
    - encoding: str = "utf-8"

    This will include a list widget on the left for the
    hosts and a basic form on the right for the other
    fields.
    """
    filename = "ping_widget.ui"

    hosts_frame: QFrame
    settings_frame: QFrame
    count_spinbox: QSpinBox
    encoding_edit: QLineEdit

    hosts_widget: BulkListWidget

    def __init__(self, data: Ping, **kwargs):
        super().__init__(data=data, **kwargs)
        # Add the list widget
        self.hosts_widget = BulkListWidget(
            data_list=self.bridge.hosts,
        )
        self.hosts_frame.layout().addWidget(self.hosts_widget)
        # Set up the static fields
        self.count_spinbox.setValue(self.bridge.count.get())
        self.count_spinbox.editingFinished.connect(self.count_edited)
        self.bridge.count.changed_value.connect(
            self.count_spinbox.setValue
        )
        setup_line_edit_data(
            self.encoding_edit,
            self.bridge.encoding,
            str,
            str,
        )

    def count_edited(self):
        self.bridge.count.put(self.count_spinbox.value())


class SimpleRowWidget(NameMixin, DataWidget):
    """
    Common behavior for these simple rows included on the various pages.
    """
    name_edit: QLineEdit
    child_button: QToolButton
    delete_button: QToolButton

    def setup_row(self):
        self.init_name()
        self.edit_filter = FrameOnEditFilter(parent=self)
        self.name_edit.installEventFilter(self.edit_filter)
        self.name_edit.textChanged.connect(self.on_name_edit_text_changed)
        self.on_name_edit_text_changed()

    def adjust_edit_filter(self):
        if self.bridge.name.get():
            self.edit_filter.set_no_edit_style(self.name_edit)
        else:
            self.edit_filter.set_edit_style(self.name_edit)

    def on_name_edit_text_changed(self, **kwargs):
        match_line_edit_text_width(self.name_edit)
        if not self.name_edit.hasFocus():
            self.adjust_edit_filter()


class ConfigurationGroupRowWidget(DesignerDisplay, SimpleRowWidget):
    """
    A row summary of a ``Configuration`` instance of a ``ConfigurationGroup``.

    You can view and edit the name from here, or delete the row.
    This will also show the class of the configuration, e.g. if it
    is a DeviceConfiguration for example, and will provide a
    button for navigation to the correct child page.

    The child_button and delete_button need to be set up by the page that
    includes this widget, as this widget has no knowledge of page navigation
    or of data outside of its ``Configuration`` instance, so it can't
    delete itself or change the page without going outside of its intended
    scope.
    """
    filename = "configuration_group_row_widget.ui"

    type_label: QLabel

    def __init__(self, data: Configuration, **kwargs):
        super().__init__(data=data, **kwargs)
        self.setup_row()
        self.type_label.setText(data.__class__.__name__)


class ComparisonRowWidget(DesignerDisplay, SimpleRowWidget):
    """
    Handle one comparison instance embedded on a configuration page.

    The attr_combo is controlled by the page this is placed in.
    It may be a PV, it may be a signal, it may be a ping result, and
    it might be a key value like "shared" with special meaning.
    """
    filename = 'comparison_row_widget.ui'

    attr_combo: QComboBox

    def __init__(self, data: Comparison, **kwargs):
        super().__init__(data=data, **kwargs)
        self.setup_row()


class GeneralComparisonWidget(DesignerDisplay, DataWidget):
    """
    Handle fields common to all Comparison data classes.
    """
    filename = 'general_comparison_widget.ui'

    invert_combo: QComboBox
    reduce_period_edit: QLineEdit
    reduce_method_combo: QComboBox
    string_combo: QComboBox
    sev_on_failure_combo: QComboBox
    if_disc_combo: QComboBox

    bool_choices = ('False', 'True')
    severity_choices = tuple(sev.name for sev in Severity)
    reduce_choices = tuple(red.name for red in ReduceMethod)

    invert_combo_items = bool_choices
    reduce_method_combo_items = reduce_choices
    string_combo_items = bool_choices
    sev_on_failure_combo_items = severity_choices
    if_disc_combo_items = severity_choices

    def __init__(self, data: Comparison, **kwargs):
        super().__init__(data=data, **kwargs)
        # Fill the generic combobox options
        for text in self.invert_combo_items:
            self.invert_combo.addItem(text)
        for text in self.reduce_method_combo_items:
            self.reduce_method_combo.addItem(text)
        for text in self.string_combo_items:
            self.string_combo.addItem(text)
        for text in self.sev_on_failure_combo_items:
            self.sev_on_failure_combo.addItem(text)
        for text in self.if_disc_combo_items:
            self.if_disc_combo.addItem(text)
        # Set up starting values based on the dataclass values
        self.invert_combo.setCurrentIndex(int(self.bridge.invert.get()))
        reduce_period = self.bridge.reduce_period.get()
        if reduce_period is not None:
            self.reduce_period_edit.setText(str(reduce_period))
        self.reduce_method_combo.setCurrentIndex(
            self.reduce_method_combo_items.index(
                self.bridge.reduce_method.get().name
            )
        )
        string_opt = self.bridge.string.get() or False
        self.string_combo.setCurrentIndex(int(string_opt))
        self.sev_on_failure_combo.setCurrentIndex(
            self.sev_on_failure_combo_items.index(
                self.bridge.severity_on_failure.get().name
            )
        )
        self.if_disc_combo.setCurrentIndex(
            self.if_disc_combo_items.index(
                self.bridge.if_disconnected.get().name
            )
        )
        # Set up the generic item signals in order from top to bottom
        self.invert_combo.currentIndexChanged.connect(
            self.new_invert_combo
        )
        self.reduce_period_edit.textEdited.connect(
            self.new_reduce_period_edit
        )
        self.reduce_method_combo.currentTextChanged.connect(
            self.new_reduce_method_combo
        )
        self.string_combo.currentIndexChanged.connect(
            self.new_string_combo
        )
        self.sev_on_failure_combo.currentTextChanged.connect(
            self.new_sev_on_failure_combo
        )
        self.if_disc_combo.currentTextChanged.connect(
            self.new_if_disc_combo
        )

    def new_invert_combo(self, index: int) -> None:
        """
        Slot to handle user input in the generic "Invert" combo box.

        Uses the current bridge to mutate the stored dataclass.

        Parameters
        ----------
        index : int
            The index the user selects in the combo box.
        """
        self.bridge.invert.put(bool(index))

    def new_reduce_period_edit(self, value: str) -> None:
        """
        Slot to handle user intput in the generic "Reduce Period" line edit.

        Tries to interpet user input as a float. If this is not possible,
        the period will not be updated.

        Uses the current bridge to mutate the stored dataclass.

        Parameters
        ----------
        value : str
            The string contents of the line edit.
        """
        try:
            value = float(value)
        except Exception:
            pass
        else:
            self.bridge.reduce_period.put(value)

    def new_reduce_method_combo(self, value: str) -> None:
        """
        Slot to handle user input in the generic "Reduce Method" combo box.

        Uses the current bridge to mutate the stored dataclass.

        Parameters
        ----------
        value : str
            The string contents of the combo box.
        """
        self.bridge.reduce_method.put(ReduceMethod[value])

    def new_string_combo(self, index: int) -> None:
        """
        Slot to handle user input in the generic "String" combo box.

        Uses the current bridge to mutate the stored dataclass.

        Parameters
        ----------
        index : int
            The integer index of the combo box.
        """
        self.bridge.string.put(bool(index))

    def new_sev_on_failure_combo(self, value: str) -> None:
        """
        Slot to handle user input in the "Severity on Failure" combo box.

        Uses the current bridge to mutate the stored dataclass.

        Parameters
        ----------
        value : str
            The string contents of the combo box.
        """
        self.bridge.severity_on_failure.put(Severity[value])

    def new_if_disc_combo(self, value: str):
        """
        Slot to handle user input in the "If Disconnected" combo box.

        Uses the current bridge to mutate the stored dataclass.

        Parameters
        ----------
        value : str
            The string contents of the combo box.
        """
        self.bridge.if_disconnected.put(Severity[value])


class EqualsComparisonWidget(DesignerDisplay, DataWidget):
    """
    Handle fields and graphics unique to the Equals comparison.
    """
    filename = 'equals_comparison_widget.ui'
    label_to_type: Dict[str, type] = {
        'float': float,
        'integer': int,
        'bool': bool,
        'string': str,
    }
    type_to_label: Dict[type, str] = {
        value: key for key, value in label_to_type.items()
    }
    cast_from_user_str: Dict[type, Callable[[str], bool]] = {
        tp: tp for tp in type_to_label
    }
    cast_from_user_str[bool] = user_string_to_bool

    value_edit: QLabel
    range_label: QLabel
    atol_label: QLabel
    atol_edit: QLineEdit
    rtol_label: QLabel
    rtol_edit: QLineEdit
    data_type_label: QLabel
    data_type_combo: QComboBox
    comp_symbol_label: QLabel

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_equals_widget()

    def setup_equals_widget(self) -> None:
        """
        Do all the setup needed to make this widget functional.

        Things handled here:
        - Set up the data type selection to know whether or not
          atol/rtol/range means anything and so that we can allow
          things like numeric strings. Use this selection to cast
          the input from the value text box.
        - Fill in the starting values for atol and rtol.
        - Connect the various edit widgets to their correspoinding
          data fields
        - Set up the range_label for a summary of the allowed range
        """
        for option in self.label_to_type:
            self.data_type_combo.addItem(option)
        setup_line_edit_data(
            line_edit=self.value_edit,
            value_obj=self.bridge.value,
            from_str=self.value_from_str,
            to_str=str,
        )
        setup_line_edit_data(
            line_edit=self.atol_edit,
            value_obj=self.bridge.atol,
            from_str=float,
            to_str=str,
        )
        setup_line_edit_data(
            line_edit=self.rtol_edit,
            value_obj=self.bridge.rtol,
            from_str=float,
            to_str=str,
        )
        starting_value = self.bridge.value.get()
        self.data_type_combo.setCurrentText(
            self.type_to_label[type(starting_value)]
        )
        self.update_range_label(starting_value)
        self.data_type_combo.currentTextChanged.connect(self.new_gui_type)
        self.bridge.value.changed_value.connect(self.update_range_label)
        self.bridge.atol.changed_value.connect(self.update_range_label)
        self.bridge.rtol.changed_value.connect(self.update_range_label)

    def update_range_label(self, *args, **kwargs) -> None:
        """
        Update the range label as appropriate.

        If our value is an int or float, this will do calculations
        using the atol and rtol to report the tolerance
        of the range to the user.

        If our value is a bool, this will summarize whether our
        value is being interpretted as True or False.
        """
        value = self.bridge.value.get()
        if not isinstance(value, (int, float, bool)):
            return
        if isinstance(value, bool):
            text = f' ({value})'
        else:
            atol = self.bridge.atol.get() or 0
            rtol = self.bridge.rtol.get() or 0

            diff = atol + abs(rtol * value)
            text = f'± {diff:.3g}'
        self.range_label.setText(text)

    def value_from_str(
        self,
        value: Optional[str] = None,
        gui_type_str: Optional[str] = None,
    ) -> PrimitiveType:
        """
        Convert our line edit value into a string based on the combobox.

        Parameters
        ----------
        value : str, optional
            The text contents of our line edit.
        gui_type_str : str, optional
            The text contents of our combobox.

        Returns
        -------
        converted : Any
            The casted datatype.
        """
        if value is None:
            value = self.value_edit.text()
        if gui_type_str is None:
            gui_type_str = self.data_type_combo.currentText()
        type_cast = self.cast_from_user_str[self.label_to_type[gui_type_str]]
        return type_cast(value)

    def new_gui_type(self, gui_type_str: str) -> None:
        """
        Slot for when the user changes the GUI data type.

        Re-interprets our value as the selected type. This will
        update the current value in the bridge as appropriate.

        If we have a numeric type, we'll enable the range and
        tolerance widgets. Otherwise, we'll disable them.

        Parameters
        ----------
        gui_type_str : str
            The user's text input from the data type combobox.
        """
        gui_type = self.label_to_type[gui_type_str]
        # Try the gui value first
        try:
            new_value = self.value_from_str(gui_type_str=gui_type_str)
        except ValueError:
            # Try the bridge value second, or give up
            try:
                new_value = gui_type(self.bridge.value.get())
            except ValueError:
                new_value = None
        if new_value is not None:
            self.bridge.value.put(new_value)
        self.range_label.setVisible(gui_type in (int, float, bool))
        tol_vis = gui_type in (int, float)
        self.atol_label.setVisible(tol_vis)
        self.atol_edit.setVisible(tol_vis)
        self.rtol_label.setVisible(tol_vis)
        self.rtol_edit.setVisible(tol_vis)


class NotEqualsComparisonWidget(EqualsComparisonWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.comp_symbol_label.setText('≠')
