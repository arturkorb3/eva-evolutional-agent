# EVA tab-completion for bash and zsh.
# Loaded automatically once you run `./run.sh install` (it sources this file from
# your ~/.bashrc or ~/.zshrc). To enable by hand for the current shell:
#     source ./completions/eva.bash
_eva_complete() {
  # Only complete the first word (the sub-command); leave task text / flags free.
  if [ "${COMP_CWORD:-1}" -ne 1 ]; then return 0; fi
  local cur="${COMP_WORDS[COMP_CWORD]}"
  local cmds="build install uninstall status work improve review evolve rollback unlock reseed paste shell fix-perms help"
  COMPREPLY=( $(compgen -W "$cmds" -- "$cur") )
}
# zsh can drive bash-style completers via bashcompinit.
if [ -n "${ZSH_VERSION:-}" ]; then
  autoload -Uz bashcompinit 2>/dev/null && bashcompinit 2>/dev/null
fi
complete -F _eva_complete eva 2>/dev/null || true
