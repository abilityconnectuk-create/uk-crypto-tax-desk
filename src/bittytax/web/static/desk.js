(function () {
  var root = document.documentElement;
  var stored = localStorage.getItem("ukctd-theme");
  if (stored === "dark" || stored === "light") {
    root.setAttribute("data-theme", stored);
  }
  if (localStorage.getItem("ukctd-nav") === "collapsed") {
    root.classList.add("nav-collapsed");
  }

  function themeNow() {
    var set = root.getAttribute("data-theme");
    if (set === "dark" || set === "light") {
      return set;
    }
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var themeBtn = document.querySelector(".theme-btn");
    if (themeBtn) {
      themeBtn.setAttribute("aria-pressed", themeNow() === "dark" ? "true" : "false");
      themeBtn.addEventListener("click", function () {
        var next = themeNow() === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        localStorage.setItem("ukctd-theme", next);
        themeBtn.setAttribute("aria-pressed", next === "dark" ? "true" : "false");
        themeBtn.setAttribute("aria-label", next === "dark" ? "Switch to light mode" : "Dark mode");
      });
    }

    var collapse = document.querySelector(".collapse-btn");
    if (collapse) {
      collapse.setAttribute("aria-pressed", root.classList.contains("nav-collapsed") ? "true" : "false");
      collapse.addEventListener("click", function () {
        var on = root.classList.toggle("nav-collapsed");
        localStorage.setItem("ukctd-nav", on ? "collapsed" : "open");
        collapse.setAttribute("aria-pressed", on ? "true" : "false");
        collapse.setAttribute("aria-label", on ? "Expand sidebar" : "Collapse sidebar");
      });
    }

    var menu = document.querySelector(".menu-btn");
    var backdrop = document.querySelector(".backdrop");
    function closeDrawer() {
      document.body.classList.remove("nav-open");
      if (menu) {
        menu.setAttribute("aria-expanded", "false");
        menu.focus();
      }
    }
    function openDrawer() {
      document.body.classList.add("nav-open");
      if (menu) {
        menu.setAttribute("aria-expanded", "true");
      }
      var first = document.querySelector("#sidebar a");
      if (first) {
        first.focus();
      }
    }
    if (menu) {
      menu.addEventListener("click", function () {
        if (document.body.classList.contains("nav-open")) {
          closeDrawer();
        } else {
          openDrawer();
        }
      });
    }
    if (backdrop) {
      backdrop.addEventListener("click", closeDrawer);
    }

    var year = document.getElementById("top-year");
    if (year && year.form) {
      year.addEventListener("change", function () {
        year.form.submit();
      });
    }

    document.querySelectorAll("main table").forEach(function (table) {
      var heads = table.querySelectorAll("thead th");
      if (!heads.length) {
        return;
      }
      table.classList.add("cards");
      table.querySelectorAll("tbody tr").forEach(function (row) {
        row.querySelectorAll("td").forEach(function (cell, index) {
          if (heads[index] && !cell.getAttribute("data-label")) {
            cell.setAttribute("data-label", heads[index].textContent.replace(/\s+/g, " ").trim());
          }
        });
      });
    });

    var holdingSearch = document.getElementById("holding-search");
    if (holdingSearch) {
      holdingSearch.addEventListener("input", function () {
        var text = holdingSearch.value.toLowerCase();
        document.querySelectorAll("#holdings tbody tr").forEach(function (row) {
          var show = row.textContent.toLowerCase().indexOf(text) !== -1;
          row.hidden = !show;
        });
      });
    }

    var txSearch = document.querySelector(".js-live");
    if (txSearch && txSearch.form) {
      var timer = 0;
      txSearch.addEventListener("input", function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(function () {
          txSearch.form.submit();
        }, 280);
      });
    }

    document.querySelectorAll(".js-auto").forEach(function (field) {
      field.addEventListener("change", function () {
        if (field.form) {
          field.form.submit();
        }
      });
    });

    wirePanel();
    wireImport();
    wireParsers();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && document.body.classList.contains("nav-open")) {
      document.body.classList.remove("nav-open");
      var menu = document.querySelector(".menu-btn");
      if (menu) {
        menu.setAttribute("aria-expanded", "false");
        menu.focus();
      }
    }
    if (event.key !== "Tab" || !document.body.classList.contains("nav-open")) {
      return;
    }
    var sidebar = document.getElementById("sidebar");
    if (!sidebar) {
      return;
    }
    var items = sidebar.querySelectorAll("a, button");
    if (!items.length) {
      return;
    }
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  var lastLink = null;

  function wirePanel() {
    var dialog = document.getElementById("tx-dialog");
    if (!dialog) {
      return;
    }
    dialog.addEventListener("close", function () {
      if (lastLink) {
        lastLink.focus();
      }
    });
    dialog.addEventListener("click", function (event) {
      var closer = event.target.closest("[data-close-panel]");
      if (closer && dialog.open) {
        event.preventDefault();
        dialog.close();
      }
    });
    document.querySelectorAll("a.tx-row").forEach(function (link) {
      link.addEventListener("click", function (event) {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
          return;
        }
        event.preventDefault();
        lastLink = link;
        openPanel(dialog, link.getAttribute("href"));
      });
    });
  }

  function openPanel(dialog, href) {
    var body = document.getElementById("tx-dialog-body");
    body.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      window.location.href = href;
      return;
    }
    fetch(href, { headers: { Accept: "text/html" } })
      .then(function (response) {
        return response.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var panel = doc.getElementById("tx-panel");
        body.textContent = "";
        if (!panel) {
          body.textContent = "That transaction could not be opened.";
          return;
        }
        body.appendChild(document.importNode(panel, true));
        var close = body.querySelector("[data-close-panel]");
        if (close) {
          close.focus();
        }
      })
      .catch(function () {
        window.location.href = href;
      });
  }

  function wireImport() {
    var form = document.getElementById("import-form");
    var input = document.getElementById("file");
    var drop = document.getElementById("drop");
    var detected = document.getElementById("detected");
    if (!form || !input) {
      return;
    }
    form.addEventListener("submit", function () {
      form.classList.add("is-busy");
      var bar = document.querySelectorAll(".progress li");
      if (bar[2]) {
        bar[2].classList.add("on");
      }
    });
    input.addEventListener("change", function () {
      if (input.files && input.files[0]) {
        detectFile(input.files[0], detected);
      }
    });
    if (drop) {
      ["dragenter", "dragover"].forEach(function (name) {
        drop.addEventListener(name, function (event) {
          event.preventDefault();
          drop.classList.add("over");
        });
      });
      drop.addEventListener("dragleave", function () {
        drop.classList.remove("over");
      });
      drop.addEventListener("drop", function (event) {
        event.preventDefault();
        drop.classList.remove("over");
        var file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
        if (!file) {
          return;
        }
        var transfer = new DataTransfer();
        transfer.items.add(file);
        input.files = transfer.files;
        detectFile(file, detected);
      });
    }
  }

  function detectFile(file, detected) {
    var token = document.querySelector('meta[name="csrf-token"]');
    var data = new FormData();
    data.append("file", file, file.name);
    if (token) {
      data.append("csrf", token.getAttribute("content") || "");
    }
    if (detected) {
      detected.textContent = "Checking the header…";
    }
    var bar = document.querySelectorAll(".progress li");
    if (bar[1]) {
      bar[1].classList.add("on");
    }
    fetch("/import/detect", { method: "POST", body: data })
      .then(function (response) {
        return response.json();
      })
      .then(function (payload) {
        if (!detected) {
          return;
        }
        if (payload.ok === "yes") {
          detected.textContent = "Detected source: " + payload.name + (payload.group ? " (" + payload.group + ")." : ".");
        } else {
          detected.textContent = payload.error || "No parser recognised those headers.";
        }
      })
      .catch(function () {
        if (detected) {
          detected.textContent = "The file is ready to import.";
        }
      });
  }

  function wireParsers() {
    var list = document.getElementById("parser-list");
    var search = document.getElementById("parser-search");
    if (!list) {
      return;
    }
    var group = "all";
    function apply() {
      var text = search ? search.value.toLowerCase() : "";
      list.querySelectorAll("li").forEach(function (item) {
        var name = item.getAttribute("data-name") || "";
        var kind = item.getAttribute("data-group") || "";
        var show = (group === "all" || kind === group) && name.indexOf(text) !== -1;
        item.hidden = !show;
      });
    }
    document.querySelectorAll("[data-parser-group]").forEach(function (button) {
      button.addEventListener("click", function () {
        group = button.getAttribute("data-parser-group") || "all";
        document.querySelectorAll("[data-parser-group]").forEach(function (other) {
          other.setAttribute("aria-pressed", other === button ? "true" : "false");
        });
        apply();
      });
    });
    if (search) {
      search.addEventListener("input", apply);
    }
  }
})();
