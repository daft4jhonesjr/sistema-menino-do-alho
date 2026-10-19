/**
 * Formulário de importação de vendas (página dedicada ou gaveta na listagem).
 */
(function (global) {
  'use strict';

  function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  function renderErrosFeedback(container, data) {
    if (!container) return;
    var erros = data.erros_detalhados || [];
    var sucesso = data.sucesso || 0;
    var errosCount = data.erros || erros.length;
    var ignorados = data.ignorados || 0;
    var intro;
    if (sucesso > 0) {
      intro =
        'Importação parcial: <strong>' +
        sucesso +
        '</strong> nova(s) venda(s) importada(s).' +
        (ignorados > 0 ? ' <strong>' + ignorados + '</strong> ignorada(s) (duplicadas).' : '') +
        ' <strong>' +
        errosCount +
        '</strong> erro(s). Corrija os itens abaixo e importe novamente.';
    } else {
      intro =
        '<strong>' +
        errosCount +
        '</strong> erro(s) encontrado(s).' +
        (ignorados > 0 ? ' <strong>' + ignorados + '</strong> ignorada(s) (duplicadas).' : '') +
        ' Confira as mensagens abaixo, ajuste sua planilha e importe novamente.';
    }
    var items = erros
      .map(function (msg) {
        return (
          '<li class="text-sm text-red-800 dark:text-red-300 flex items-start">' +
          '<span class="text-red-500 mr-2 flex-shrink-0">•</span> ' +
          escapeHtml(msg) +
          '</li>'
        );
      })
      .join('');
    container.innerHTML =
      '<div class="bg-red-50 dark:bg-red-900/20 border-2 border-red-300 dark:border-red-800 rounded-lg p-5 shadow-sm">' +
      '<div class="flex items-start"><div class="flex-shrink-0">' +
      '<i class="fas fa-exclamation-triangle text-red-600 dark:text-red-400 text-2xl"></i></div>' +
      '<div class="ml-4 flex-1 min-w-0">' +
      '<h3 class="text-lg font-bold text-red-800 dark:text-red-300 mb-2">Ops! Encontramos alguns probleminhas</h3>' +
      '<p class="text-red-700 dark:text-red-400 mb-3">' +
      intro +
      '</p>' +
      '<ul class="space-y-2 max-h-64 overflow-y-auto pr-2">' +
      items +
      '</ul></div></div></div>';
    container.classList.remove('hidden');
    container.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function initImportarVendasForm(root, options) {
    options = options || {};
    root = root || document;
    var scope = root.querySelector ? root : document;
    var fileInput = scope.querySelector('#arquivo-importar-vendas');
    var previewContainer = scope.querySelector('#preview-container');
    var previewHeader = scope.querySelector('#preview-header');
    var previewBody = scope.querySelector('#preview-body');
    var previewRowCount = scope.querySelector('#preview-row-count');
    var formImportar = scope.querySelector('#form-importar-vendas');
    var btnImportar = scope.querySelector('#btn-importar-vendas');
    var loadingImportacao = scope.querySelector('#loading-importacao');
    var feedbackWrap = scope.querySelector('#importar-vendas-feedback');

    if (!fileInput || !formImportar) return;

    var useAjax =
      options.ajax === true ||
      (formImportar.getAttribute('data-ajax') === '1' && options.ajax !== false);

    var columnOrder = [
      'cliente',
      'nome_cliente',
      'nf',
      'nota_fiscal',
      'nota',
      'produto',
      'nome_produto',
      'nome',
      'preco_venda',
      'preco',
      'preço',
      'quantidade',
      'quantidade_venda',
      'qtd',
      'qtd_venda',
      'data_venda',
      'data',
      'empresa_faturadora',
      'empresa',
      'situacao',
      'situação',
      'status',
    ];

    var columnDisplayNames = {
      cliente: 'CLIENTE',
      nome_cliente: 'CLIENTE',
      nf: 'NF',
      nota_fiscal: 'NF',
      nota: 'NF',
      produto: 'PRODUTO',
      nome_produto: 'PRODUTO',
      nome: 'PRODUTO',
      preco_venda: 'PREÇO VENDA',
      preco: 'PREÇO VENDA',
      preço: 'PREÇO VENDA',
      quantidade: 'QTD VENDA',
      quantidade_venda: 'QTD VENDA',
      qtd: 'QTD VENDA',
      qtd_venda: 'QTD VENDA',
      data_venda: 'DATA',
      data: 'DATA',
      empresa_faturadora: 'EMPRESA',
      empresa: 'EMPRESA',
      situacao: 'SITUAÇÃO',
      situação: 'SITUAÇÃO',
      status: 'SITUAÇÃO',
    };

    function normalizarNomeColuna(s) {
      if (!s) return '';
      s = String(s).trim().toLowerCase();
      s = s.replace(/\s+/g, '_');
      s = s.replace('ç', 'c').replace('ã', 'a').replace('á', 'a').replace('à', 'a');
      return s;
    }

    function formatarPreco(val) {
      if (!val) return 'R$ 0,00';
      var num = parseFloat(val);
      if (isNaN(num)) return 'R$ 0,00';
      return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(num);
    }

    function formatarSituacao(val) {
      if (!val) return 'PENDENTE';
      var s = String(val).trim().toUpperCase();
      return s === 'PAGO' ? 'PAGO' : 'PENDENTE';
    }

    function getSituacaoClass(val) {
      var s = formatarSituacao(val);
      return s === 'PAGO'
        ? 'px-2 py-1 text-xs font-semibold rounded-full bg-green-100 dark:bg-green-900/40 text-green-800 dark:text-green-300'
        : 'px-2 py-1 text-xs font-semibold rounded-full bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-300';
    }

    function processAndDisplayData(jsonData) {
      if (!previewContainer || !previewHeader || !previewBody) return;
      if (jsonData.length === 0) {
        previewContainer.classList.add('hidden');
        return;
      }

      var headers = jsonData[0].map(function (h) {
        return normalizarNomeColuna(h);
      });

      var columnIndices = {};
      headers.forEach(function (header, index) {
        columnIndices[header] = index;
      });

      var foundColumns = [];
      columnOrder.forEach(function (colName) {
        var normalized = normalizarNomeColuna(colName);
        if (Object.prototype.hasOwnProperty.call(columnIndices, normalized) && foundColumns.indexOf(normalized) === -1) {
          foundColumns.push(normalized);
        }
      });

      headers.forEach(function (header) {
        if (foundColumns.indexOf(header) === -1) {
          foundColumns.push(header);
        }
      });

      previewHeader.innerHTML = '';
      foundColumns.forEach(function (colName) {
        var th = document.createElement('th');
        th.className =
          'px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider';
        th.textContent = columnDisplayNames[colName] || colName.toUpperCase();
        previewHeader.appendChild(th);
      });

      previewBody.innerHTML = '';
      var maxRows = Math.min(5, jsonData.length - 1);
      if (previewRowCount) previewRowCount.textContent = String(maxRows);

      for (var i = 1; i <= maxRows && i < jsonData.length; i++) {
        var row = jsonData[i];
        var tr = document.createElement('tr');
        tr.className = 'hover:bg-gray-50 dark:hover:bg-gray-700/50';

        foundColumns.forEach(function (colName) {
          var td = document.createElement('td');
          var colIndex = columnIndices[colName];
          var cellValue =
            colIndex !== undefined && row[colIndex] !== undefined ? String(row[colIndex]) : '';

          var normalizedCol = normalizarNomeColuna(colName);
          if (normalizedCol.indexOf('preco') !== -1 || normalizedCol.indexOf('preço') !== -1) {
            td.className =
              'px-6 py-4 whitespace-nowrap text-sm text-gray-900 dark:text-gray-100 text-right font-medium';
            td.textContent = formatarPreco(cellValue);
          } else if (
            normalizedCol.indexOf('situacao') !== -1 ||
            normalizedCol.indexOf('situação') !== -1 ||
            normalizedCol.indexOf('status') !== -1
          ) {
            td.className = 'px-6 py-4 whitespace-nowrap';
            var situacao = formatarSituacao(cellValue);
            var span = document.createElement('span');
            span.className = getSituacaoClass(cellValue);
            span.textContent = situacao;
            td.appendChild(span);
          } else if (normalizedCol.indexOf('quantidade') !== -1 || normalizedCol.indexOf('qtd') !== -1) {
            td.className =
              'px-6 py-4 whitespace-nowrap text-sm text-gray-900 dark:text-gray-100 text-right';
            td.textContent = cellValue || '0';
          } else {
            td.className = 'px-6 py-4 whitespace-nowrap text-sm text-gray-900 dark:text-gray-100';
            td.textContent = cellValue || '-';
          }

          tr.appendChild(td);
        });

        previewBody.appendChild(tr);
      }

      previewContainer.classList.remove('hidden');
    }

    fileInput.addEventListener('change', function (e) {
      var file = e.target.files[0];

      if (!file) {
        if (previewContainer) previewContainer.classList.add('hidden');
        return;
      }

      var reader = new FileReader();

      if (file.name.endsWith('.csv')) {
        reader.onload = function (ev) {
          try {
            var text = ev.target.result;
            var lines = text.split('\n').filter(function (line) {
              return line.trim();
            });

            if (lines.length === 0) {
              previewContainer.classList.add('hidden');
              return;
            }

            var parseCSVLine = function (line) {
              var result = [];
              var current = '';
              var inQuotes = false;

              for (var j = 0; j < line.length; j++) {
                var char = line[j];

                if (char === '"') {
                  inQuotes = !inQuotes;
                } else if ((char === ',' || char === ';') && !inQuotes) {
                  result.push(current.trim());
                  current = '';
                } else {
                  current += char;
                }
              }
              result.push(current.trim());
              return result;
            };

            var jsonData = lines.map(function (line) {
              return parseCSVLine(line);
            });
            processAndDisplayData(jsonData);
          } catch (err) {
            console.error('Erro ao ler CSV:', err);
            if (previewContainer) previewContainer.classList.add('hidden');
            alert('Erro ao ler o arquivo CSV. Verifique se o formato está correto.');
          }
        };
        reader.readAsText(file);
      } else {
        reader.onload = function (ev) {
          try {
            if (typeof global.XLSX === 'undefined') {
              alert('Biblioteca de planilhas ainda não carregou. Aguarde um instante e tente novamente.');
              return;
            }
            var data = new Uint8Array(ev.target.result);
            var workbook = global.XLSX.read(data, { type: 'array' });

            var firstSheetName = workbook.SheetNames[0];
            var worksheet = workbook.Sheets[firstSheetName];
            var jsonData = global.XLSX.utils.sheet_to_json(worksheet, { header: 1 });
            processAndDisplayData(jsonData);
          } catch (err) {
            console.error('Erro ao ler arquivo:', err);
            if (previewContainer) previewContainer.classList.add('hidden');
            alert('Erro ao ler o arquivo. Verifique se o formato está correto.');
          }
        };
        reader.readAsArrayBuffer(file);
      }
    });

    function setLoading(on) {
      if (!btnImportar || !loadingImportacao) return;
      if (on) {
        loadingImportacao.classList.remove('hidden');
        btnImportar.innerHTML =
          '<i data-lucide="loader-2" class="w-4 h-4 mr-2 animate-spin"></i>Importando...';
        btnImportar.style.pointerEvents = 'none';
        btnImportar.classList.add('opacity-50');
      } else {
        loadingImportacao.classList.add('hidden');
        btnImportar.innerHTML = '<i data-lucide="upload" class="w-4 h-4 mr-2"></i>Importar';
        btnImportar.style.pointerEvents = '';
        btnImportar.classList.remove('opacity-50');
      }
      if (typeof global.lucide !== 'undefined') global.lucide.createIcons();
    }

    formImportar.addEventListener('submit', function (e) {
      if (!useAjax) {
        setLoading(true);
        return;
      }

      e.preventDefault();
      setLoading(true);

      var fd = new FormData(formImportar);
      fetch(formImportar.action, {
        method: 'POST',
        body: fd,
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'same-origin',
      })
        .then(function (res) {
          return res.json().catch(function () {
            throw new Error('Resposta inválida do servidor.');
          });
        })
        .then(function (data) {
          setLoading(false);
          if (data.ok && data.recarregar) {
            if (typeof options.onSuccess === 'function') {
              options.onSuccess(data);
            } else if (typeof global.Swal !== 'undefined') {
              global.Swal.fire({
                icon: 'success',
                title: 'Importação concluída',
                text: data.mensagem || 'Vendas importadas com sucesso.',
                confirmButtonColor: '#047857',
              }).then(function () {
                global.location.reload();
              });
            } else {
              alert(data.mensagem || 'Importação concluída.');
              global.location.reload();
            }
            return;
          }
          if (data.aviso) {
            if (typeof global.Swal !== 'undefined') {
              global.Swal.fire({
                icon: 'warning',
                title: 'Nenhuma venda importada',
                text: data.mensagem || 'A planilha estava vazia ou não encontramos dados válidos.',
                confirmButtonColor: '#047857',
              });
            } else {
              alert(data.mensagem || 'Nenhuma venda importada.');
            }
            return;
          }
          var fb = feedbackWrap;
          if (fb) {
            fb.innerHTML = '';
            renderErrosFeedback(fb, data);
          }
        })
        .catch(function (err) {
          setLoading(false);
          console.error(err);
          alert('Não foi possível importar. Tente novamente.');
        });
    });
  }

  global.initImportarVendasForm = initImportarVendasForm;
})(typeof window !== 'undefined' ? window : this);
