// rmdesk's bar button: a click opens rmdesk, or focuses it when it is already open. When
// rmdesk isn't installed yet, the click opens a terminal that installs it from this folder
// (it asks first). The work is done by open.sh next to this file.
import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.ruffbudda.rmdesk"

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  // this plugin's folder on disk, from the file URL of open.sh
  readonly property string openScript: decodeURIComponent(
    Qt.resolvedUrl("open.sh").toString().replace(/^file:\/\//, ""))

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""  // nf-fa-tablet
    tooltipText: strings.t("tooltip")
    onPressed: function(mouseButton) {
      if (!root.bar || mouseButton !== Qt.LeftButton) return
      root.bar.run(Util.shellQuote(root.openScript))
    }
  }
}
