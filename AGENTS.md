# AGENTS.md — busca_AMHPDF

## Testes locais e GitHub Actions

- Execute localmente os testes e as validações aplicáveis antes de abrir ou
  atualizar um PR ou publicar diretamente na branch de deploy. Registre no PR
  os comandos, os resultados e qualquer validação pendente.
- O PR não executa essa validação. Reserve GitHub Actions para deploy na VPS,
  incluindo artefatos necessários à publicação e a conferência de saúde do
  serviço publicado. Mantenha testes, lint e revisão de PR fora do Actions.

Quando aplicável, confira localmente a compilação, os testes e os fluxos afetados. A disponibilidade do Railway pode ser conferida no próprio serviço.
