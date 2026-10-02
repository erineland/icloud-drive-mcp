// Extract text from a PDF using macOS PDFKit via the JXA ObjC bridge.
// Used because poppler/pdftotext is not installed on this machine and
// Python has no Quartz bindings in the system interpreter.
//
//   osascript -l JavaScript pdftext.js <abs-path> [firstPage] [lastPage]
//
// Pages are 1-indexed and inclusive. Omit them to extract the whole document.
ObjC.import('Quartz')

function run(argv) {
  var path = argv[0]
  if (!path) return 'ERROR: no path given'

  var url = $.NSURL.fileURLWithPath(path)
  var doc = $.PDFDocument.alloc.initWithURL(url)
  if (!doc || doc.js === undefined) return 'ERROR: could not open PDF'

  var count = doc.pageCount
  if (count === undefined) return 'ERROR: could not read PDF (encrypted or damaged?)'

  // Whole document: PDFDocument.string is far faster than page-by-page.
  if (argv.length < 2) {
    var all = doc.string
    return all.js === undefined ? '' : all.js
  }

  var first = Math.max(1, parseInt(argv[1], 10) || 1)
  var last = Math.min(count, parseInt(argv[2], 10) || first)
  var out = []
  for (var i = first; i <= last; i++) {
    var page = doc.pageAtIndex(i - 1)
    if (!page) continue
    var s = page.string
    out.push('--- page ' + i + ' ---\n' + (s.js === undefined ? '' : s.js))
  }
  return out.join('\n')
}
