(function () {
  "use strict";

  window.card = function (title, subtitle, innerHtml) {
    return `
      <div class="bg-white rounded-2xl shadow-sm border border-gray-100 p-5 card-hover">
        <div class="flex items-start justify-between gap-3">
          <div>
            <h3 class="text-lg font-bold text-gray-900">${title}</h3>
            <p class="text-sm text-gray-500">${subtitle || ""}</p>
          </div>
        </div>
        <div class="mt-4">${innerHtml || ""}</div>
      </div>
    `;
  };

  window.badge = function (text) {
    return `<span class="px-2 py-1 rounded-full text-xs bg-purple-50 text-purple-700 border border-purple-100">${text}</span>`;
  };

  window.btn = function (text, onclick, kind = "primary") {
    const cls =
      kind === "primary"
        ? "bg-purple-600 hover:bg-purple-700 text-white"
        : "bg-gray-100 hover:bg-gray-200 text-gray-900";
    return `<button class="px-3 py-2 rounded-xl text-sm font-semibold ${cls}" onclick="${onclick}">${text}</button>`;
  };
})();