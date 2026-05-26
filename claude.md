I was dumb to start the Tauri wrapper without making sure everything works fine. Here's the two things I want you to do today:
1. Currently The .env is hard-wired with no ability to change in the front end, so I want you to:
    1. With the current Frontend Add a separate account management page, I want to you to add one account management access page in the front end and place under the left chat session bar, It should show account avatar and account name, and clicking it lead to account setting page that reroutes to account settings page. The account settings page should also feel like the main chat in the middle, but allows you change account settings. See 2.
    2. The account setting page Does the following: Allows the user to change the DEEPSEEK_API_KEY and NOTION_API_KEY environment variable. Use encrypted transmission method. Show in default "***" and allow the user to see the full and change with one click; The account setting page should allow user to change their avatar by uploading their own, allow user to change their user name, and how Reggia should call the user. This information should be stored in .env as well.
    3. The CLAUDE.md in /backend/chat_workspace need to be updated to stream to Claude Code in the chatworkspace to make the system more responsive. Basically every Hanze right now in backend/chat_workspace/CLAUDE.md needs to be changed to USER_NAME in .env

2. Write a fancy looking starting page that asks user to upload their avatar, create their username, and upload DEEPSEEK_API_KEY and NOTION_API_KEY (if these values are None)

Constraints:
1. Reggia is so far only and will only be single user so no need to use Databases to store user information;
2. Make reference to Anthropic's own frontend user setting design and the initial welcome page, DO NOT CHANGE THE CURRENT COLOR SCHEME OF REGGIA
3. For this task DO NOT read anything under /desktop folder