# EVA tab-completion for PowerShell.
# Loaded automatically once you run `.\run.ps1 install` (it dot-sources this file
# from your $PROFILE). To enable by hand for the current session:
#     . .\completions\eva.ps1
Register-ArgumentCompleter -Native -CommandName eva -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $cmds = @(
        'build', 'install', 'uninstall', 'status', 'work', 'improve', 'review',
        'evolve', 'rollback', 'unlock', 'reseed', 'paste', 'shell', 'fix-perms', 'help'
    )
    # Only suggest the sub-command (first argument); leave task text / flags free.
    $elements = $commandAst.CommandElements
    $completingCommand = ($elements.Count -eq 1) -or ($elements.Count -eq 2 -and $wordToComplete -ne '')
    if ($completingCommand) {
        $cmds |
            Where-Object { $_ -like "$wordToComplete*" } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
    }
}
