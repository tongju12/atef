"""
Widget classes designed for atef configuration.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os.path
from functools import partial
from pathlib import Path
from pprint import pprint
from typing import Any, ClassVar, List, Optional, Type, Union

from apischema import deserialize, serialize
from qtpy.QtCore import QEvent, QObject, QTimer
from qtpy.QtWidgets import (QAction, QComboBox, QFileDialog, QFormLayout,
                            QHBoxLayout, QLabel, QLayout, QLineEdit,
                            QMainWindow, QMessageBox, QPlainTextEdit,
                            QPushButton, QTabWidget, QTreeWidget,
                            QTreeWidgetItem, QVBoxLayout, QWidget)
from qtpy.uic import loadUiType

from ..check import (Comparison, Configuration, ConfigurationFile,
                     DeviceConfiguration, Equals, IdentifierAndComparison,
                     PVConfiguration, Severity)
from ..qt_helpers import QDataclassBridge, QDataclassList
from ..reduce import ReduceMethod

logger = logging.getLogger(__name__)


class AtefCfgDisplay:
    """Helper class for loading the .ui files and adding logic."""
    filename: str

    def __init_subclass__(cls):
        """Read the file when the class is created"""
        super().__init_subclass__()
        cls.ui_form, _ = loadUiType(
            str(Path(__file__).parent.parent / 'ui' / cls.filename)
        )

    def __init__(self, *args, **kwargs):
        """Apply the file to this widget when the instance is created"""
        super().__init__(*args, **kwargs)
        self.ui_form.setupUi(self, self)

    def retranslateUi(self, *args, **kwargs):
        """Required function for setupUi to work in __init__"""
        self.ui_form.retranslateUi(self, *args, **kwargs)


class Window(AtefCfgDisplay, QMainWindow):
    """
    Main atef config window

    Has a tab widget for editing multiple files at once, and contains
    the menu bar for facilitating saving/loading.
    """
    filename = 'config_window.ui'
    user_default_filename = 'untitled'
    user_filename_ext = 'json'

    tab_widget: QTabWidget
    action_new_file: QAction
    action_open_file: QAction
    action_save: QAction
    action_save_as: QAction
    action_print_dataclass: QAction
    action_print_serialized: QAction

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowTitle('atef config')
        self.action_new_file.triggered.connect(self.new_file)
        self.action_open_file.triggered.connect(self.open_file)
        self.action_save.triggered.connect(self.save)
        self.action_save_as.triggered.connect(self.save_as)
        self.action_print_dataclass.triggered.connect(self.print_dataclass)
        self.action_print_serialized.triggered.connect(self.print_serialized)
        QTimer.singleShot(0, self.welcome_user)

    def welcome_user(self):
        """
        On open, ask the user what they'd like to do (new config? load?)

        TODO: only show when we don't get a file cli argument to start.
        """
        welcome_box = QMessageBox()
        welcome_box.setIcon(QMessageBox.Question)
        welcome_box.setWindowTitle('Welcome')
        welcome_box.setText('Welcome to atef config!')
        welcome_box.setInformativeText('Please select a startup action')
        open_button = welcome_box.addButton(QMessageBox.Open)
        new_button = welcome_box.addButton('New', QMessageBox.AcceptRole)
        welcome_box.addButton(QMessageBox.Close)
        open_button.clicked.connect(self.open_file)
        new_button.clicked.connect(self.new_file)
        welcome_box.exec()

    def get_tab_name(self, filename: Optional[str] = None):
        """
        Get a standardized tab name from a filename.
        """
        if filename is None:
            filename = self.user_default_filename
        if '.' not in filename:
            filename = '.'.join((filename, self.user_filename_ext))
        return os.path.basename(filename)

    def set_current_tab_name(self, filename: str):
        """
        Set the title of the current tab based on the filename.
        """
        self.tab_widget.setTabText(
            self.tab_widget.currentIndex(),
            self.get_tab_name(filename),
        )

    def get_current_tree(self) -> Tree:
        """
        Return the widget of the current open tab.
        """
        return self.tab_widget.currentWidget()

    def new_file(self, *args, **kwargs):
        """
        Create and populate a new edit tab.

        The parameters are open as to accept inputs from any signal.
        """
        widget = Tree(config_file=ConfigurationFile(configs=[]))
        self.tab_widget.addTab(widget, self.get_tab_name())

    def open_file(self, *args, filename: Optional[str] = None, **kwargs):
        """
        Open an existing file and create a new tab containing it.

        The parameters are open as to accept inputs from any signal.

        Parameters
        ----------
        filename : str, optional
            The name to save the file as. If omitted, a dialog will
            appear to prompt the user for a filepath.
        """
        if filename is None:
            filename, _ = QFileDialog.getOpenFileName(
                parent=self,
                caption='Select a config',
                filter='Json Files (*.json)',
            )
        if not filename:
            return
        with open(filename, 'r') as fd:
            serialized = json.load(fd)
        data = deserialize(ConfigurationFile, serialized)
        widget = Tree(config_file=data, full_path=filename)
        self.tab_widget.addTab(widget, self.get_tab_name(filename))
        self.tab_widget.setCurrentIndex(self.tab_widget.count()-1)

    def save(self, *args, **kwargs):
        """
        Save the currently selected tab to the last used filename.

        Reverts back to save_as if no such filename exists.

        The parameters are open as to accept inputs from any signal.
        """
        current_tree = self.get_current_tree()
        self.save_as(filename=current_tree.full_path)

    def save_as(self, *args, filename: Optional[str] = None, **kwargs):
        """
        Save the currently selected tab, to a specific filename.

        The parameters are open as to accept inputs from any signal.

        Parameters
        ----------
        filename : str, optional
            The name to save the file as. If omitted, a dialog will
            appear to prompt the user for a filepath.
        """
        current_tree = self.get_current_tree()
        serialized = self.serialize_tree(current_tree)
        if serialized is None:
            return
        if filename is None:
            filename, _ = QFileDialog.getSaveFileName(
                parent=self,
                caption='Save as',
                filter='Json Files (*.json)',
            )
        try:
            with open(filename, 'w') as fd:
                json.dump(serialized, fd, indent=2)
        except OSError:
            logger.exception(f'Error saving file {filename}')
        else:
            self.set_current_tab_name(filename)
            current_tree.full_path = filename

    def serialize_tree(self, tree: Tree) -> dict:
        """
        Return the serialized data from a Tree widget.
        """
        try:
            return serialize(
                ConfigurationFile,
                tree.bridge.data,
            )
        except Exception:
            logger.exception('Error serializing file')

    def print_dataclass(self, *args, **kwargs):
        """
        Print the dataclass of the current tab.

        The parameters are open as to accept inputs from any signal.
        """
        pprint(self.get_current_tree().bridge.data)

    def print_serialized(self, *args, **kwargs):
        """
        Print the serialized data structure of the current tab.

        The parameters are open as to accept inputs from any signal.
        """
        pprint(self.serialize_tree(self.get_current_tree()))


class Tree(AtefCfgDisplay, QWidget):
    """
    The main per-file widget as a "native" view into the file.

    Consists of a tree visualization on the left that can be selected through
    to choose which part of the tree to edit in the widget space on the right.

    Parameters
    ----------
    config_file : ConfigurationFile
        The config file object to use to build the tree.
    full_path : str, optional
        The full path to the last file used to save or load the tree.
    """
    filename = 'config_tree.ui'

    bridge: QDataclassBridge
    tree_widget: QTreeWidget

    full_path: str

    def __init__(
        self,
        *args,
        config_file: ConfigurationFile,
        full_path: Optional[str] = None,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.bridge = QDataclassBridge(config_file, parent=self)
        self.full_path = full_path
        self.last_selection: Optional[AtefItem] = None
        self.built_widgets = set()
        self.assemble_tree()
        self.show_selected_display(self.overview_item)
        self.tree_widget.itemPressed.connect(self.show_selected_display)

    def assemble_tree(self):
        """
        On startup, create the full tree.
        """
        self.tree_widget.setColumnCount(2)
        self.tree_widget.setHeaderLabels(['Node', 'Type'])
        self.overview_item = AtefItem(
            widget_class=Overview,
            widget_args=[self.bridge.configs, self.tree_widget],
            name='Overview',
            func_name='overview'
        )
        self.tree_widget.insertTopLevelItem(0, self.overview_item)

    def show_selected_display(self, item: AtefItem, *args, **kwargs):
        """
        Show the proper widget on the right when a tree row is selected.

        This works by hiding the previous widget and showing the new
        selection, creating the widget object if needed.

        Parameters
        ----------
        item : AtefItem
            The selected item in the tree. This contains information like
            the textual annotation, cached widget references, and
            arguments for creating a new widget if needed.
        """
        if item is self.last_selection:
            return
        if self.last_selection is not None:
            self.last_selection.get_widget().setVisible(False)
        widget = item.get_widget()
        if widget not in self.built_widgets:
            self.layout().addWidget(widget)
            self.built_widgets.add(widget)
        widget.setVisible(True)
        self.last_selection = item


class AtefItem(QTreeWidgetItem):
    """
    A QTreeWidget item with some convenience methods.

    Facilitates the widget creation/caching mechanisms.
    """
    widget_class: type[QWidget]
    widget_args: list[Any]
    widget_cached: QWidget

    def __init__(
        self,
        *args,
        widget_class: type[QWidget],
        widget_args: Optional[list[Any]],
        name: str,
        func_name: Optional[str] = None,
        append_item_arg: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.setText(0, name)
        if func_name is not None:
            self.setText(1, func_name)
        self.widget_class = widget_class
        self.widget_args = widget_args or []
        if append_item_arg:
            self.widget_args.append(self)
        self.widget_cached = self.widget_class(*self.widget_args)

    def get_widget(self) -> QWidget:
        """
        Return the edit widget associated with this tree node.
        """
        return self.widget_cached


class Overview(AtefCfgDisplay, QWidget):
    """
    A view of all the top-level "Configuration" objects.

    This widget allows us to browse our config names, classes, and
    descriptions, as well as add new configs.

    TODO: add a way to delete configs.

    Parameters
    ----------
    config_file : ConfigurationFile
        A reference to the full config file dataclass to read from
        and update to as we do edits.
    tree_ref : QTreeWidget
        A reference to the entire tree widget so we can update the
        top-level names in the tree as they are edited here.
    """
    filename = 'config_overview.ui'

    add_device_button: QPushButton
    add_pv_button: QPushButton
    scroll_content: QWidget

    config_list: QDataclassList
    tree_ref: QTreeWidget
    row_count: int

    def __init__(
        self,
        config_list: QDataclassList,
        tree_ref: QTreeWidget,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.config_list = config_list
        self.tree_ref = tree_ref
        self.row_count = 0
        self.initialize_overview()
        self.add_device_button.clicked.connect(self.add_device_config)
        self.add_pv_button.clicked.connect(self.add_pv_config)

    def initialize_overview(self):
        """
        Read the configuration data and create the overview rows.
        """
        for config in self.config_list.get():
            if isinstance(config, DeviceConfiguration):
                self.add_device_config(config=config, update_data=False)
            elif isinstance(config, PVConfiguration):
                self.add_pv_config(config=config, update_data=False)
            else:
                raise RuntimeError(
                    f'{config} is not a valid config!'
                )

    def add_device_config(
        self,
        checked: Optional[bool] = None,
        config: Optional[DeviceConfiguration] = None,
        update_data: bool = True,
    ):
        """
        Add a device config row to the tree and to the overview.

        This method exists so that we can make the "add_device_button" work.

        Parameters
        ----------
        checked : bool
            Expected argument from a QPushButton, unused
        config : DeviceConfiguration, optional
            The device configuration to add. If omitted, we'll create
            a blank config.
        update_data : bool, optional
            If True, the default, mutates the dataclass.
            Set to False during the initial reading of the file.
        """
        if config is None:
            config = DeviceConfiguration()
        self.add_config(config, update_data=update_data)

    def add_pv_config(
        self,
        checked: Optional[bool] = None,
        config: Optional[PVConfiguration] = None,
        update_data: bool = True,
    ):
        """
        Add a pv config row to the tree and to the overview.

        This method exists so that we can make the "add_pv_button" work.

        Parameters
        ----------
        checked : bool
            Expected argument from a QPushButton, unused
        config : PVConfiguration, optional
            The PV configuration to add. If omitted, we'll create
            a blank config.
        update_data : bool, optional
            If True, the default, mutates the dataclass.
            Set to False during the initial reading of the file.
        """
        if config is None:
            config = PVConfiguration()
        self.add_config(config, update_data=update_data)

    def add_config(
        self,
        config: Union[DeviceConfiguration, PVConfiguration],
        update_data: bool = True,
    ):
        """
        Add an existing config to the tree and to the overview.

        This is the core method that modifies the tree and adds the row
        widget.

        Parameters
        ----------
        config : Configuration
            A single configuration object.
        update_data : bool, optional
            If True, the default, mutates the dataclass.
            Set to False during the initial reading of the file.
        """
        if isinstance(config, DeviceConfiguration):
            func_name = 'device config'
        else:
            func_name = 'pv config'
        row = OverviewRow(config)
        self.scroll_content.layout().insertWidget(
            self.row_count,
            row,
        )
        item = AtefItem(
            widget_class=Group,
            widget_args=[row.bridge],
            name=config.name or 'untitled',
            func_name=func_name,
            append_item_arg=True,
        )
        self.tree_ref.addTopLevelItem(item)
        self.row_count += 1

        # If either of the widgets change the name, update tree
        row.bridge.name.changed_value.connect(
            partial(item.setText, 0)
        )
        # Note: this is the only place in the UI where
        # we add new config data
        if update_data:
            self.config_list.append(config)


class ConfigTextMixin:
    """
    A mix-in class for proper name and desc widget handling.

    Does the following:
    - sets up self.bridge to take updates from and send
      updates to self.name_edit and self.desc_edit
    - makes self.desc_edit expand/contract to match the
      available text
    """
    bridge: QDataclassBridge
    name_edit: QLineEdit
    desc_edit: QPlainTextEdit

    def initialize_config_text(self):
        """
        Call this in the mixed-in class to establish the config text.

        Requires self.bridge, self.name_edit, and self.desc_edit
        to be instantiated and available.
        """
        self.initialize_config_name()
        self.initialize_config_desc()

    def initialize_config_name(self):
        """
        Call this in the mixed-in class to establish the config name only.

        Requires self.bridge and self.name_edit
        to be instantiated and available.
        """
        # Load starting text
        load_name = self.bridge.name.get() or ''
        self.name_edit.setText(load_name)
        # Setup the name edit
        self.name_edit.textEdited.connect(self.update_saved_name)
        self.bridge.name.changed_value.connect(self.name_edit.setText)

    def initialize_config_desc(self):
        """
        Call this in the mixed-in class to establish the config desc only.

        Requires self.bridge and self.desc_edit
        to be instantiated and available.
        """
        # Load starting text
        load_desc = self.bridge.description.get() or ''
        self.last_desc = load_desc
        self.desc_edit.setPlainText(load_desc)

        # Setup the desc edit
        self.desc_edit.textChanged.connect(self.update_saved_desc)
        self.bridge.description.changed_value.connect(self.apply_new_desc)
        self.update_text_height()
        self.desc_edit.textChanged.connect(self.update_text_height)

    def update_saved_name(self, name: str):
        """
        When the user edits the name, write to the config.
        """
        self.bridge.name.put(name)

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


class OverviewRow(ConfigTextMixin, AtefCfgDisplay, QWidget):
    """
    A single row in the overview widget.

    This displays and provides means to edit the name and description
    of a single configuration.

    Parameters
    ----------
    config : Configuration
        The full configuration associated with this row, so that we can
        read and edit the name and description.
    """
    filename = 'config_overview_row.ui'

    bridge: QDataclassBridge

    name_edit: QLineEdit
    config_type: QLabel
    lock_button: QPushButton
    desc_edit: QPlainTextEdit

    def __init__(
        self,
        config: Union[DeviceConfiguration, PVConfiguration],
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.bridge = QDataclassBridge(config, parent=self)
        self.initialize_row()

    def initialize_row(self):
        """
        Set up all the logic and starting state of the row widget.
        """
        self.initialize_config_text()
        if isinstance(self.bridge.data, DeviceConfiguration):
            self.config_type.setText('Device Config')
        else:
            self.config_type.setText('PV Config')
        # Setup the lock button
        self.lock_button.toggled.connect(self.handle_locking)
        if self.name_edit.text():
            # Start locked if we are reading from file
            self.lock_button.toggle()

    def lock_editing(self, locked: bool):
        """
        Set the checked state of the "locked" button as the user would.
        """
        self.lock_button.setChecked(locked)

    def handle_locking(self, locked: bool):
        """
        When the checked state of the "locked" button changes, make it so.

        When locked, the boxes will be read only and have an indicated visual change.
        When unlocked, the boxes will be writable and have the default look and feel.

        It is expected that the user won't edit these a lot, and that it is easier
        to browse through the rows with the non-edit style.
        """
        self.name_edit.setReadOnly(locked)
        self.name_edit.setFrame(not locked)
        self.desc_edit.setReadOnly(locked)
        if locked:
            self.desc_edit.setFrameShape(self.desc_edit.NoFrame)
            self.setStyleSheet(
                "QLineEdit, QPlainTextEdit { background: transparent }"
            )
        else:
            self.desc_edit.setFrameShape(self.desc_edit.StyledPanel)
            self.setStyleSheet(
                "QLineEdit, QPlainTextEdit { background: white }"
            )


class Group(ConfigTextMixin, AtefCfgDisplay, QWidget):
    """
    The group of checklists and devices associated with a Configuration.

    From this widget we can edit name/description, add tags,
    add devices, and add checklists to the Configuration.

    Parameters
    ----------
    bridge : QDataclassBridge
        A dataclass bridge to an atef.check.Configuration dataclass.
        This will be used to update the dataclass and to listen for
        dataclass updates.
    tree_item : AtefItem
        The item in the atef config tree view that corresponds with
        this widget. We'll use this to modify the tree as the user
        adds, removes, or edits the contained checklists.
    """
    filename = 'config_group.ui'

    name_edit: QLineEdit
    desc_edit: QPlainTextEdit
    tags_content: QVBoxLayout
    add_tag_button: QPushButton
    devices_container: QWidget
    devices_content: QVBoxLayout
    add_devices_button: QPushButton
    checklists_container: QWidget
    checklists_content: QVBoxLayout
    add_checklist_button: QPushButton
    line_between_adds: QWidget

    def __init__(
        self,
        bridge: QDataclassBridge,
        tree_item: AtefItem,
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.bridge = bridge
        self.tree_item = tree_item
        self.initialize_group()

    def initialize_group(self) -> None:
        """
        Perform first-time setup for the widget.

        - Set up the name and desc text using the standard behavior
        - Set up the tag widget and make it functional
        - Hide the devices layout for PVConfiguration
        - Set up the devices widget for DeviceConfiguration
        - Set up the list of checklists widget
        """
        self.initialize_config_text()
        tags_list = StrList(
            data_list=self.bridge.tags,
            layout=QHBoxLayout(),
        )
        self.tags_content.addWidget(tags_list)
        self.add_tag_button.clicked.connect(
            partial(tags_list.add_item, '')
        )
        if isinstance(self.bridge.data, PVConfiguration):
            self.devices_container.hide()
            self.line_between_adds.hide()
        else:
            devices_list = StrList(
                data_list=self.bridge.devices,
                layout=QVBoxLayout(),
            )
            self.devices_content.addWidget(devices_list)
            self.add_devices_button.clicked.connect(
                partial(devices_list.add_item, '')
            )
        self.checklist_list = NamedDataclassList(
            data_list=self.bridge.checklist,
            layout=QVBoxLayout(),
        )
        self.checklists_content.addWidget(self.checklist_list)
        for bridge in self.checklist_list.bridges:
            self.setup_checklist_item_bridge(bridge)
        self.add_checklist_button.clicked.connect(self.add_checklist)

    def setup_checklist_item_bridge(self, bridge: QDataclassBridge) -> None:
        """
        Set up a single checklist item with a dataclass bridge.

        Parameters
        ----------
        bridge : QDataclassBridge
            A dataclass bridge to an instance of
            atef.check.IdentifierAndComparison
        """
        item = AtefItem(
            widget_class=IdAndCompWidget,
            widget_args=[bridge, type(self.bridge.data)],
            name=bridge.name.get() or 'untitled',
            func_name='checklist',
            append_item_arg=True,
        )
        self.tree_item.addChild(item)
        bridge.name.changed_value.connect(
            partial(item.setText, 0)
        )

    def add_checklist(
        self,
        checked: Optional[bool] = None,
        id_and_comp: Optional[IdentifierAndComparison] = None,
    ) -> None:
        """
        Add a new or existing checklist to the list of checklists.

        Parameters
        ----------
        checked : bool, optional
            This argument is unused, but it will be sent by various button
            widgets via the "clicked" signal so it must be present.
        id_and_comp : IdentifierAndComparison, optional
            The checklist to add. If omitted, we'll create a blank checklist.
        """
        if id_and_comp is None:
            id_and_comp = IdentifierAndComparison()
        bridge = self.checklist_list.add_item(id_and_comp)
        self.setup_checklist_item_bridge(bridge)
        # TODO make the delete button work
        # new_row.del_button.clicked.connect


class StrList(QWidget):
    """
    A widget used to modify the str variant of QDataclassList.

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
    widgets: List[StrListElem]

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
        checked: Optional[bool] = None,
        init: bool = False,
    ) -> StrListElem:
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

        Returns
        -------
        strlistelem : StrListElem
            The widget created by this function call.
        """
        new_widget = StrListElem(starting_value, parent=self)
        self.widgets.append(new_widget)
        if not init:
            self.data_list.append(starting_value)
        self.layout().addWidget(new_widget)
        new_widget.line_edit.textEdited.connect(
            partial(self.save_item_update, new_widget)
        )
        new_widget.del_button.clicked.connect(
            partial(self.remove_item, new_widget)
        )
        return new_widget

    def save_item_update(self, item: StrListElem, new_value: str) -> None:
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

    def remove_item(self, item: StrListElem, checked: bool) -> None:
        """
        Update the dataclass as appropriate when the user removes a value.

        Parameters
        ----------
        item : StrListElem
            The widget that the user has clicked the delete button for.
        checked : bool, optional
            This argument is unused, but it will be sent by various button
            widgets via the "clicked" signal so it must be present.
        """
        index = self.widgets.index(item)
        self.widgets.remove(item)
        self.data_list.remove_index(index)
        item.deleteLater()


class NamedDataclassList(StrList):
    """
    A widget used to modify a QDataclassList with named dataclass elements.

    A named dataclass is any dataclass element with a str "name" field.
    This widget will allow us to add elements to the list by name,
    display the names, modify the names, add blank entries, etc.

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
    bridges = List[QDataclassBridge]

    def __init__(self, *args, **kwargs):
        self.bridges = []
        super().__init__(*args, **kwargs)

    def add_item(
        self,
        starting_value: Any,
        checked: Optional[bool] = None,
        init: bool = False,
    ) -> QDataclassBridge:
        """
        Create and add new editable widget element to this widget's layout.

        This can either be an existing dataclass for the list to keep
        track of, or it can be used to add a new dataclass to the list.

        This method will also set up the signals and slots for the new widget.

        Unlike the parent class, this will set up and return a
        QDataclassBridge that can be used to manage edits and updates to the
        dataclass. This bridge will be configured to link edits to the
        text widget with edits to the name field.

        Parameters
        ----------
        starting_value : Any dataclass
            The starting dataclass for the new widget element.
            This should be the actual dataclass for tracking existing
            dataclasses.
        checked : bool, optional
            This argument is unused, but it will be sent by various button
            widgets via the "clicked" signal so it must be present.
        init : bool, optional
            Whether or not this is the initial initialization of this widget.
            This will be set to True in __init__ so that we don't mutate
            the underlying dataclass. False, the default, means that we're
            adding a new dataclass to the list, which means we should
            definitely append it.

        Returns
        -------
        strlistelem : StrListElem
            The widget created by this function call.
        """
        if not init:
            self.data_list.append(starting_value)
        new_widget = super().add_item(
            starting_value=starting_value.name,
            checked=checked,
            init=True,
        )
        bridge = QDataclassBridge(starting_value, parent=self)
        self._setup_bridge_signals(bridge, new_widget)
        self.bridges.append(bridge)
        return bridge

    def _setup_bridge_signals(
        self,
        bridge: QDataclassBridge,
        widget: StrListElem,
    ) -> None:
        """
        Set up all the signals needed for a widget element and its bridge.

        Parameters
        ----------
        bridge : QDataclassBridge
            A bridge to the dataclass associated with the widget element.
        widget : StrListElem
            The widget element to link.
        """
        bridge.name.changed_value.connect(
            widget.line_edit.setText
        )

    def save_item_update(self, item: StrListElem, new_value: str) -> None:
        """
        Update the dataclass as appropriate when the user submits a new value.

        Unlike the parent class, this will update the name field rather than
        replace the entire string object.

        Parameters
        ----------
        item : StrListElem
            The widget that the user has edited.
        new_value : str
            The value that the user has submitted.
        """
        index = self.widgets.index(item)
        self.bridges[index].name.put(new_value)

    def remove_item(self, item: StrListElem, checked: bool) -> None:
        """
        Update the dataclass as appropriate when the user removes a value.

        Parameters
        ----------
        item : StrListElem
            The widget that the user has clicked the delete button for.
        checked : bool, optional
            This argument is unused, but it will be sent by various button
            widgets via the "clicked" signal so it must be present.
        """
        index = self.widgets.index(item)
        super().remove_item(item=item, checked=checked)
        bridge = self.bridges[index]
        bridge.deleteLater()
        del self.bridges[index]

    def update_item_bridge(
        self,
        old_bridge: QDataclassBridge,
        new_bridge: QDataclassBridge,
    ) -> None:
        """
        Replace an existing bridge with a new bridge.

        This can be useful if you need to change out an entire dataclass,
        as may need to be done when the user requests a change of
        Comparison type.

        Internally, this handles any widget-specific setup of the new bridge
        and as much teardown as we can do to the old bridge.

        Parameters
        ----------
        old_bridge : QDataclassBridge
            The existing bridge that we'd like to replace.
        new_bridge : QDataclassBridge
            The new bridge that we'd like to replace it with.
        """
        index = self.bridges.index(old_bridge)
        self.bridges[index] = new_bridge
        new_bridge.setParent(self)
        self._setup_bridge_signals(
            new_bridge,
            self.widgets[index],
        )
        old_bridge.deleteLater()
        self.data_list.put_to_index(index, new_bridge.data)


class StrListElem(AtefCfgDisplay, QWidget):
    """
    A single element for the StrList widget.

    Has a QLineEdit for changing the text and a delete button.
    Changes its style to no frame when it has text and is out of focus.
    Only shows the delete button when the text is empty.

    The StrList widget is responsible for connecting this widget
    to the dataclass bridge.
    """
    filename = 'str_list_elem.ui'

    line_edit: QLineEdit
    del_button: QPushButton

    def __init__(self, start_text: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.line_edit.setText(start_text)
        self.line_edit.setFrame(not start_text)
        edit_filter = FrameOnEditFilter(parent=self)
        self.line_edit.installEventFilter(edit_filter)
        self.on_text_changed(start_text)
        self.line_edit.textChanged.connect(self.on_text_changed)

    def on_text_changed(self, text: str) -> None:
        """
        Edit our various visual elements when the user edits the text field.

        This will do all of the following:
        - make the delete button show only when the text field is empty
        - adjust the size of the text field to be roughly the size of the
          string we've inputted
        """
        # Show or hide the del button as needed
        self.del_button.setVisible(not text)
        # Adjust the width to match the text
        font_metrics = self.line_edit.fontMetrics()
        width = font_metrics.boundingRect(text).width()
        self.line_edit.setFixedWidth(max(width + 10, 40))


class FrameOnEditFilter(QObject):
    """
    A QLineEdit event filter for editing vs not editing style handling.

    This will make the QLineEdit look like a QLabel when the user is
    not editing it.
    """
    def eventFilter(self, object: QLineEdit, event: QEvent) -> bool:
        if event.type() == QEvent.FocusIn:
            object.setFrame(True)
            object.setReadOnly(False)
            return True
        if event.type() == QEvent.FocusOut:
            if object.text():
                object.setFrame(False)
            object.setReadOnly(True)
            return True
        return False


class IdAndCompWidget(ConfigTextMixin, AtefCfgDisplay, QWidget):
    """
    A widget to manage the ids and comparisons associated with a checklist.

    Parameters
    ----------
    bridge : QDataclassBridge
        A dataclass bridge to an atef.check.IdentifierAndComparison instance.
    config_type : DeviceConfiguration or PVConfiguration
        The type associated with this configuration. There are two types of
        checklists: those that reference ophyd objects, and those that
        reference PVs.
    """
    filename = 'id_and_comp.ui'

    name_edit: QLineEdit
    id_label: QLabel
    id_content: QVBoxLayout
    add_id_button: QPushButton
    comp_label: QLabel
    comp_content: QVBoxLayout
    add_comp_button: QPushButton

    bridge: QDataclassBridge
    config_type: Type[Configuration]

    def __init__(
        self,
        bridge: QDataclassBridge,
        config_type: Type[Configuration],
        tree_item: AtefItem,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.bridge = bridge
        self.config_type = config_type
        self.tree_item = tree_item
        self.bridge_item_map = {}
        self.initialize_idcomp()

    def initialize_idcomp(self) -> None:
        """
        Perform first-time setup of this widget.

        Does the following:
        - Connects the name field with the dataclass
        - Sets up the list of PVs or Devices and adjusts the label
        - Sets up the list of comparisons
        """
        # Connect the name to the dataclass
        self.initialize_config_name()
        # Set up editing of the identifiers list
        identifiers_list = StrList(
            data_list=self.bridge.ids,
            layout=QVBoxLayout(),
        )
        self.id_content.addWidget(identifiers_list)
        self.add_id_button.clicked.connect(
            partial(identifiers_list.add_item, '')
        )
        # Adjust the identifier text appropriately for config type
        if issubclass(self.config_type, DeviceConfiguration):
            self.id_label.setText('Device Signals')
            self.add_id_button.setText('Add Signal')
        elif issubclass(self.config_type, PVConfiguration):
            self.id_label.setText('PV Names')
            self.add_id_button.setText('Add PV')
        self.comparison_list = NamedDataclassList(
            data_list=self.bridge.comparisons,
            layout=QVBoxLayout(),
        )
        self.comp_content.addWidget(self.comparison_list)
        for bridge in self.comparison_list.bridges:
            self.setup_comparison_item_bridge(bridge)
        self.add_comp_button.clicked.connect(self.add_comparison)

    def setup_comparison_item_bridge(self, bridge: QDataclassBridge) -> None:
        """
        Create the AtefItem associated with a bridge and set it up.

        These items handle the tree entry and loading of the subscreen.

        Parameters
        ----------
        bridge : QDataclassBridge
            A dataclass bridge to an instance of
            atef.check.IdentifierAndComparison
        """
        item = AtefItem(
            widget_class=CompView,
            widget_args=[bridge, self],
            name=bridge.name.get() or 'untitled',
            func_name='comparison',
        )
        self.tree_item.addChild(item)
        self.bridge_item_map[bridge] = item
        self._setup_bridge_signals(bridge)

    def _setup_bridge_signals(self, bridge: QDataclassBridge) -> None:
        """
        Set up all the relevant signals for a QDataclassBridge.

        Currently, this just makes it so that when you edit the
        name field, the tree entry updates its text.

        Parameters
        ----------
        bridge : QDataclassBridge

        """
        item = self.bridge_item_map[bridge]
        bridge.name.changed_value.connect(
            partial(item.setText, 0)
        )

    def update_comparison_bridge(
        self,
        old_bridge: QDataclassBridge,
        new_bridge: QDataclassBridge,
    ) -> None:
        """
        Swap out an underlying QDataclassBridge.

        This is used when the user wants to change a comparison's class.

        Parameters
        ----------
        old_bridge : QDataclassBridge
            The previous existing bridge.
        new_bridge : QDataclassBridge
            The new bridge to replace it with.
        """
        self.comparison_list.update_item_bridge(old_bridge, new_bridge)
        item = self.bridge_item_map[old_bridge]
        self.bridge_item_map[new_bridge] = item
        self._setup_bridge_signals(new_bridge)

    def add_comparison(
        self,
        checked: Optional[bool] = None,
        comparison: Optional[Comparison] = None,
    ) -> None:
        """
        Add a new or existing comparison to the list.

        Parameters
        ----------
        checked : bool, optional
            This argument is unused, but it will be sent by various button
            widgets via the "clicked" signal so it must be present.
        comparison : Comparison subclass, optional
            The specific comparison instance to add.
            If omitted, we'll create a blank atef.check.Equals instance
            as a default.
        """
        if comparison is None:
            # Empty default
            comparison = Equals()
        bridge = self.comparison_list.add_item(comparison)
        self.setup_comparison_item_bridge(bridge)
        # TODO make the delete button work
        # new_row.del_button.clicked.connect


class CompView(ConfigTextMixin, AtefCfgDisplay, QWidget):
    """
    Widget to view and edit a single Comparison subclass.

    This contains some generic fields common to all Comparison
    subclasses, and then a placeholder for Comparison-specific
    widgets to be loaded into.

    Comparison subclasses can be registered for use here by
    calling the register_comparison classmethod, which is
    called automatically in the CompMixin helper class.

    Parameters
    ----------
    bridge : QDataclassBridge
        A dataclass bridge that points to a subclass of Comparison.
    id_and_comp : IdAndCompWidget
        The widget that created and owns this widget.
        This is used in place of parent to be more robust to
        structural changes for us to access the checklist when we
        need to change the data type.
    """
    filename = 'comp_view.ui'

    name_edit: QLineEdit
    desc_edit: QPlainTextEdit
    comp_type_combo: QComboBox
    specific_content: QVBoxLayout
    generic_content: QFormLayout
    invert_combo: QComboBox
    reduce_period_edit: QLineEdit
    reduce_method_combo: QComboBox
    string_combo: QComboBox
    sev_on_failure_combo: QComboBox
    if_disc_combo: QComboBox

    specific_comparison_widgets: ClassVar[dict[type: type]] = {}
    data_types: ClassVar[dict[str: type]] = {}

    bool_choices = ('False', 'True')
    severity_choices = tuple(sev.name for sev in Severity)
    reduce_choices = tuple(red.name for red in ReduceMethod)

    invert_combo_items = bool_choices
    reduce_method_combo_items = reduce_choices
    string_combo_items = bool_choices
    sev_on_failure_combo_items = severity_choices
    if_disc_combo_items = severity_choices

    @classmethod
    def register_comparison(
        cls,
        dataclass_type: Type[Comparison],
        widget_type: Type[QWidget],
    ) -> None:
        """
        Register a comparison to be added to the combobox options.

        Parameters
        ----------
        dataclass_type : any Comparison subclass
            The comparison type to register.
        widget_type : QWidget
            The widget to load for that comparison type. Must accept
            a QDataclassBridge to the comparison instance as its
            first positional argument.
        """
        cls.specific_comparison_widgets[dataclass_type] = widget_type
        cls.data_types[dataclass_type.__name__] = dataclass_type

    def __init__(
        self,
        bridge: QDataclassBridge,
        id_and_comp: IdAndCompWidget,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.bridge = bridge
        self.id_and_comp = id_and_comp
        self.comparison_setup_done = False
        self.initialize_comp_view()

    def initialize_comp_view(self) -> None:
        """
        First time setup for the widget.

        - Populates the combo box with options
        - Switches the combobox to match the loaded type
        - Calls change_comparison_type with the initial type
        - Sets up the combobox signals and slots
        """
        last_added_index = 0
        for type_name, data_type in self.data_types.items():
            self.comp_type_combo.addItem(type_name)
            if isinstance(self.bridge.data, data_type):
                self.comp_type_combo.setCurrentIndex(last_added_index)
            last_added_index += 1
        self.change_comparison_type(type(self.bridge.data))
        self.comp_type_combo.currentTextChanged.connect(
            self._comp_type_from_combobox,
        )

    def _comp_type_from_combobox(self, type_name: str) -> None:
        """
        Call change_comparison_type as a combobox slot.

        Changes the argument to the type name as a string rather than as
        the type itself.

        Parameters
        ----------
        type_name : str
            The string name of the comparison type.
        """
        return self.change_comparison_type(self.data_types[type_name])

    def change_comparison_type(self, new_type: Type[Comparison]) -> None:
        """
        Switch the comparison from one type to another.

        On the first pass, this just needs to connect the generic
        widgets to our dataclass and setup the type-specific edit
        widgets by delegating to the appropriate widget class.

        On subsequent calls, this also needs to do the following:
        - create new dataclass
        - create new bridge
        - transfer over any matching fields
        - update the parent bridge about our new dataclass
        - clean up the old widget
        - swap out the edit widgets for the appropriate version
        - connect everything to the new bridge'

        Parameters
        ----------
        new_type : Comparison subclass
            The class to switch our comparison type to.
        """
        if self.comparison_setup_done:
            # Clean up the previous widget
            self.specific_widget.deleteLater()
            # Clean up the previous bridge
            old_bridge = self.bridge
            old_data = self.bridge.data
            # Create a new dataclass, transferring over any compatible data
            new_data = cast_dataclass(old_data, new_type)
            # Create a new bridge, seeded with the new dataclass
            new_bridge = QDataclassBridge(new_data)
            self.bridge = new_bridge
            # Replace our bridge in the parent as appropriate
            self.id_and_comp.update_comparison_bridge(old_bridge, new_bridge)
        # Redo the text setup with the new bridge (or maybe the first time)
        self.initialize_config_text()
        # Set up the widget specific items
        try:
            widget_class = self.specific_comparison_widgets[new_type]
        except KeyError:
            raise TypeError(
                f'{new_type} is not a registered type for CompView. '
                'Currently the registered types are '
                f'{tuple(self.specific_comparison_widgets)}'
            )
        self.specific_widget = widget_class(self.bridge)
        self.specific_content.addWidget(self.specific_widget)

        if not self.comparison_setup_done:
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
            self.comparison_setup_done = True

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
        the period will be stored as zero.

        Uses the current bridge to mutate the stored dataclass.

        Parameters
        ----------
        value : str
            The string contents of the line edit.
        """
        try:
            value = float(value)
        except Exception:
            value = 0
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
    new_fields = dataclasses.fields(new_type)
    field_names = set(field.name for field in new_fields)
    new_kwargs = {
        key: value for key, value in dataclasses.asdict(data).items()
        if key in field_names
    }
    return new_type(**new_kwargs)


class CompMixin:
    """
    Helper class for creating comparison widgets.

    Include as one of the parent classes and define the data_type classvar
    to ensure the widget is included as an option in CompView.
    """
    data_type: ClassVar[type]

    def __init_subclass__(cls, *args, **kwargs):
        super().__init_subclass__(*args, **kwargs)
        CompView.register_comparison(cls.data_type, cls)


# This class should be replaced by a real "Equals" widget
class EqualsWidget(CompMixin, QLabel):
    data_type = Equals

    def __init__(self, bridge: QDataclassBridge, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setText('This is just a placeholder for "Equals" for testing.')
