-- doc2md droplet
--
-- Drag documents onto this app (in the Dock, the Desktop, or the Finder toolbar)
-- and they are converted to Markdown in ~/Downloads/doc2md.
--
-- All the real work is in Contents/Resources/convert.sh; this file only drives it
-- once per file so the progress bar can move between them.

on run
	-- Opened with no files, which is what a double-click means. Settings is the
	-- useful thing to show; the output folder is one click away from there and is
	-- named in every completion notification anyway.
	set settingsApp to (path to home folder as text) & "Applications:doc2md Settings.app"
	try
		tell application "Finder" to get settingsApp as alias
		do shell script "open -a " & quoted form of (POSIX path of settingsApp)
		return
	end try

	-- Settings not built. Fall back to the output folder.
	try
		tell application "Finder"
			open folder ((path to downloads folder as text) & "doc2md")
			activate
		end tell
	on error
		display dialog "Nothing converted yet." & return & return & ¬
			"Drag a document onto this app's icon to convert it to Markdown." & return & ¬
			"Build the settings window with ./settings/build.sh." ¬
			buttons {"OK"} default button 1 with title "doc2md" with icon note
	end try
end run

on open droppedItems
	set engine to quoted form of (POSIX path of (path to me) & "Contents/Resources/convert.sh")
	set total to (count of droppedItems)

	set converted to 0
	set skipped to 0
	set failed to 0
	set lastPath to ""
	set firstError to ""
	set undescribed to 0
	set tokensSpent to 0
	set heldFiles to {}
	set heldCount to 0

	set progress total steps to total
	set progress completed steps to 0
	set progress description to "Converting to Markdown…"

	repeat with i from 1 to total
		set thisFile to item i of droppedItems
		set progress additional description to (my basename(POSIX path of thisFile))

		try
			set engineOutput to do shell script engine & " " & quoted form of (POSIX path of thisFile)
			set converted to converted + 1
			-- First line is the Markdown path; an UNDESCRIBED line may follow when
			-- diagrams needed the vision model and there was no key for it.
			repeat with outputLine in (paragraphs of engineOutput)
				set outputLine to outputLine as text
				if outputLine starts with "UNDESCRIBED:" then
					set undescribed to undescribed + ((text 13 thru -1 of outputLine) as integer)
				else if outputLine starts with "TOKENS:" then
					set tokensSpent to tokensSpent + ((text 8 thru -1 of outputLine) as integer)
				else if outputLine starts with "HELD:" then
					set heldCount to heldCount + ((text 6 thru -1 of outputLine) as integer)
					set end of heldFiles to (POSIX path of thisFile)
				else if outputLine is not "" then
					set lastPath to outputLine
				end if
			end repeat
		on error errorText number errorNumber
			-- convert.sh exits 3 for "not something doc2md handles", which is a
			-- skip rather than a failure; anything else is a real error.
			if errorNumber is 3 then
				set skipped to skipped + 1
			else
				set failed to failed + 1
				if firstError is "" then set firstError to errorText
			end if
		end try

		set progress completed steps to i
	end repeat

	set progress completed steps to total

	-- Above doc2md's threshold the diagrams are held rather than described, so the
	-- document is already converted and this only asks about the extra API spend.
	-- 2680 tokens per image, measured; 250000 is the free daily budget.
	if heldCount > 0 then
		set estimate to heldCount * 2680
		set pct to (round ((estimate / 250000) * 100))
		display dialog "Converted, but " & (heldCount as text) & ¬
			" diagrams were left undescribed." & return & return & ¬
			"Describing them costs about " & (estimate as text) & " tokens (" & ¬
			(pct as text) & "% of your daily budget)." ¬
			buttons {"Leave them", "Describe them"} default button 1 ¬
			with title "doc2md" with icon caution
		if button returned of result is "Describe them" then
			set progress description to "Describing diagrams…"
			set progress total steps to (count of heldFiles)
			set progress completed steps to 0
			repeat with j from 1 to (count of heldFiles)
				try
					set retryOutput to do shell script engine & " " & ¬
						quoted form of (item j of heldFiles) & " --vision-ok"
					repeat with outputLine in (paragraphs of retryOutput)
						set outputLine to outputLine as text
						if outputLine starts with "TOKENS:" then
							set tokensSpent to tokensSpent + ((text 8 thru -1 of outputLine) as integer)
						end if
					end repeat
					set heldCount to 0
				end try
				set progress completed steps to j
			end repeat
		end if
	end if

	-- A failure needs to be readable and dismissed deliberately; a success should
	-- not steal focus, so it goes to Notification Center.
	if converted is 0 then
		if failed is 0 then
			display dialog "Nothing to convert." & return & return & ¬
				"Those files are not types doc2md handles." ¬
				buttons {"OK"} default button 1 with title "doc2md" with icon caution
		else
			display dialog "Could not convert." & return & return & firstError ¬
				buttons {"OK"} default button 1 with title "doc2md" with icon stop
		end if
		return
	end if

	set summary to (converted as text) & " file"
	if converted is not 1 then set summary to summary & "s"
	set summary to summary & " converted"
	if failed > 0 then set summary to summary & ", " & (failed as text) & " failed"
	if skipped > 0 then set summary to summary & ", " & (skipped as text) & " skipped"

	-- Diagrams that OCR cannot represent are the one silent way the Markdown comes
	-- out thinner than the document, so it gets said plainly rather than buried.
	set subtitleText to "Saved to Downloads/doc2md"
	if tokensSpent > 0 then
		set subtitleText to (tokensSpent as text) & " vision tokens used"
	end if
	if undescribed > 0 then
		set subtitleText to (undescribed as text) & " diagram"
		if undescribed is not 1 then set subtitleText to subtitleText & "s"
		set subtitleText to subtitleText & " need a Gemini API key"
	end if

	try
		display notification summary with title "doc2md" subtitle subtitleText
	end try

	-- Reveal only when there is something ambiguous to look at, or a single result
	-- the user probably wants to open next.
	if failed > 0 then
		display dialog summary & return & return & firstError ¬
			buttons {"OK"} default button 1 with title "doc2md" with icon caution
	end if
end open

on basename(posixPath)
	set AppleScript's text item delimiters to "/"
	set theName to last text item of posixPath
	set AppleScript's text item delimiters to ""
	return theName
end basename
