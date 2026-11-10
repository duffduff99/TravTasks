// Small glue code. Pages are plain HTML from the server; htmx swaps them in
// without full reloads (hx-boost on <body>), and SortableJS handles dragging.

// Reload just the page content (keeps the capture box and its keyboard focus).
function refreshMain() {
  htmx.ajax("GET", location.pathname + location.search, {
    target: "#main", select: "#main", swap: "outerHTML",
  });
}

// The server sends this event after a capture (see views.capture).
document.addEventListener("refresh-main", refreshMain);

// Keep the scroll position when ticking a checkbox, but start new pages at the top.
htmx.config.scrollIntoViewOnBoost = false;
var wentToNewPage = false;
document.addEventListener("htmx:beforeRequest", function (event) {
  var config = event.detail.requestConfig;
  wentToNewPage = event.detail.boosted && config.verb === "get" && config.elt.tagName === "A";
});
document.addEventListener("htmx:afterSettle", function () {
  if (wentToNewPage) window.scrollTo(0, 0);
  wentToNewPage = false;
});

// Every <ul data-list="..."> is a drag list. Lists with the same data-group
// can pass items to each other. After a drop we tell the server, then reload
// the content: if a rule refused the move, the item snaps back and the
// refusal message appears at the top.
htmx.onLoad(function (root) {
  var lists = Array.from(root.querySelectorAll("[data-list]"));
  if (root.matches && root.matches("[data-list]")) lists.push(root);
  lists.forEach(function (list) {
    if (list.sortable) return;
    list.sortable = new Sortable(list, {
      group: list.dataset.group || list.dataset.list,
      draggable: "[data-id]",
      filter: "input, select, textarea, button, .fixed",
      preventOnFilter: false,
      delay: 200,
      delayOnTouchOnly: true,
      animation: 150,
      ghostClass: "dragging",
      onEnd: function (event) {
        var ids = Array.from(event.to.children)
          .filter(function (child) { return child.dataset.id; })
          .map(function (child) { return child.dataset.id; });
        fetch("/move", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: event.item.dataset.id, target: event.to.dataset.list, ids: ids }),
        }).then(refreshMain, refreshMain);
      },
    });
  });
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(function () {});
}
