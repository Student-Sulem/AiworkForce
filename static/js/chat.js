/* ==========================================================================
   chat.js -- the AI Employees chat screen.

   Responsibilities:
     * send a message and render the reply
     * start, rename and delete conversations
     * the auto-growing composer, with Enter to send
     * the slide-out configuration panel
     * sending a reply to the approval queue

   Every URL comes from data attributes on #chatRoot, so no path is hard-coded
   here and the URL names in marketing/urls.py stay the single source of truth.
   ========================================================================== */

(function (App) {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var root = document.getElementById('chatRoot');
    if (!root) { return; }

    var urls = {
      send: root.dataset.sendUrl,
      create: root.dataset.newUrl,
      rename: root.dataset.renameUrl,
      remove: root.dataset.deleteUrl,
      submit: root.dataset.submitUrl
    };
    var conversationId = parseInt(root.dataset.conversationId, 10);

    var log = document.getElementById('chatMessages');
    var form = document.getElementById('chatForm');
    var input = document.getElementById('chatInput');
    var sendButton = document.getElementById('chatSend');

    /* --- Rendering --------------------------------------------------------- */

    function scrollToBottom() {
      log.scrollTop = log.scrollHeight;
    }

    /* Build the same markup as partials/_chat_message.html, so a reply looks
       identical whether Django rendered it or this function did.

       Markdown is NOT parsed here. The endpoint returns `content_html`,
       already rendered and escaped by marketing/markdown.py, so there is one
       implementation of both the formatting and the safety argument. */
    function renderMessage(message, agent) {
      var article = document.createElement('article');
      article.className = 'msg msg--' + message.role;
      article.dataset.messageId = message.id;

      var isAssistant = message.role === 'assistant';
      var avatarStyle = isAssistant ? ' style="background:' + agent.color + '"' : '';
      var icon = isAssistant ? agent.icon : 'fa-user';

      var meta = '';
      var actions = '';
      if (isAssistant) {
        var sourceBadge = message.source === 'live'
          ? '<span class="badge badge--success"><i class="fa-solid fa-bolt"></i> Live model</span>'
          : '<span class="badge badge--neutral"><i class="fa-solid fa-file-lines"></i> Template reply</span>';
        var modelChip = message.model
          ? '<span class="chip chip--mono">' + App.escapeHtml(message.model) + '</span>' : '';
        var tokenNote = message.tokens
          ? '<span class="u-text-xs u-text-subtle">' + message.tokens + ' tokens</span>' : '';
        var toolChips = (message.tools || []).map(function (t) {
          return '<span class="chip chip--mono">' + App.escapeHtml(t) + '</span>';
        }).join('');

        meta = '<div class="msg__meta">' + sourceBadge + modelChip + tokenNote + toolChips + '</div>';
        actions =
          '<div class="msg__actions">' +
          '<button class="btn btn--secondary btn--sm" type="button" data-submit-message="' +
          message.id + '"><i class="fa-solid fa-clipboard-check"></i> Send for approval</button>' +
          '<button class="btn btn--ghost btn--sm" type="button" data-copy-message="' +
          message.id + '"><i class="fa-regular fa-copy"></i> Copy</button>' +
          '</div>';
      }

      var contentClass = isAssistant ? 'msg__content msg__content--rich' : 'msg__content';

      article.innerHTML =
        '<span class="msg__avatar"' + avatarStyle + '><i class="fa-solid ' + icon + '"></i></span>' +
        '<div class="msg__body">' +
        '<p class="msg__who u-m-0">' + (isAssistant ? App.escapeHtml(agent.name) : 'You') +
        '<span class="msg__time">' + App.escapeHtml(message.created_at) + '</span></p>' +
        '<div class="' + contentClass + '"></div>' + meta + actions +
        '</div>';

      var body = article.querySelector('.msg__content');
      if (isAssistant && message.content_html) {
        // Safe: the server escaped the model's text before generating any tag.
        body.innerHTML = message.content_html;
      } else {
        // What the person typed is inserted as text, never parsed as markup.
        body.textContent = message.content;
      }
      return article;
    }

    function agentIdentity() {
      var avatar = document.querySelector('.chat__header .avatar');
      var iconEl = avatar && avatar.querySelector('i');
      return {
        name: (document.querySelector('.chat__header-meta') || {}).textContent
          ? document.querySelector('.chat__header-meta').textContent.trim().split(' ')[0]
          : 'Assistant',
        color: avatar ? avatar.style.background : 'var(--color-primary)',
        icon: iconEl ? (iconEl.className.match(/fa-[a-z-]+$/) || ['fa-robot'])[0] : 'fa-robot'
      };
    }

    function showTyping(agent) {
      var el = document.createElement('article');
      el.className = 'msg msg--assistant';
      el.id = 'chatTyping';
      el.innerHTML =
        '<span class="msg__avatar" style="background:' + agent.color + '">' +
        '<i class="fa-solid ' + agent.icon + '"></i></span>' +
        '<div class="msg__body"><div class="typing" aria-label="Thinking">' +
        '<span></span><span></span><span></span></div></div>';
      log.appendChild(el);
      scrollToBottom();
    }

    function hideTyping() {
      var el = document.getElementById('chatTyping');
      if (el) { el.remove(); }
    }

    /* --- Sending ------------------------------------------------------------ */

    function send(text) {
      if (!text.trim()) { return; }

      var welcome = log.querySelector('.chat__welcome');
      if (welcome) { welcome.remove(); }

      var agent = agentIdentity();

      // Shown immediately so typing feels responsive. The timestamp is a
      // placeholder: the server's is authoritative and replaces it below,
      // which also stops the browser clock and the server clock disagreeing.
      var pending = renderMessage({
        id: 'pending', role: 'user', content: text,
        created_at: ''
      }, agent);
      log.appendChild(pending);
      scrollToBottom();

      input.value = '';
      autoGrow();
      input.disabled = true;
      sendButton.disabled = true;
      showTyping(agent);

      // 60 seconds: a live provider call can legitimately take a while.
      App.postJSON(urls.send, { conversation_id: conversationId, text: text }, 60000)
        .then(function (result) {
          hideTyping();
          // Swap the optimistic bubble for the stored one, so its id and
          // timestamp match the database.
          pending.replaceWith(renderMessage(result.user_message, agent));
          log.appendChild(renderMessage(result.assistant_message, agent));
          scrollToBottom();

          var title = document.getElementById('chatTitle');
          if (title && result.conversation_title) {
            title.textContent = result.conversation_title;
          }
          var active = document.querySelector('.chat__thread.is-active .chat__thread-title');
          if (active && result.conversation_title) {
            active.textContent = result.conversation_title;
          }
        })
        .catch(function (error) {
          hideTyping();
          App.toast(error.message, 'danger');
        })
        .finally(function () {
          input.disabled = false;
          sendButton.disabled = false;
          input.focus();
        });
    }

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      send(input.value);
    });

    // Enter sends; Shift and Enter make a new line.
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        send(input.value);
      }
    });

    function autoGrow() {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 200) + 'px';
    }
    input.addEventListener('input', autoGrow);
    autoGrow();

    App.on('click', '.chat__suggestion', function () {
      send(this.dataset.prompt);
    });

    /* --- Conversations ------------------------------------------------------ */

    App.on('click', '[data-new-conversation]', function () {
      var button = this;
      App.setLoading(button, true, 'Starting');
      App.postJSON(urls.create, { agent_id: button.dataset.newConversation })
        .then(function (result) { window.location.href = result.url; })
        .catch(function (error) {
          App.setLoading(button, false);
          App.toast(error.message, 'danger');
        });
    });

    var renameButton = document.getElementById('renameConversation');
    if (renameButton) {
      renameButton.addEventListener('click', function () {
        var current = document.getElementById('chatTitle').textContent.trim();
        var next = window.prompt('Rename this conversation', current);
        if (next === null || !next.trim()) { return; }

        App.postJSON(urls.rename, { conversation_id: conversationId, title: next.trim() })
          .then(function (result) {
            document.getElementById('chatTitle').textContent = result.title;
            var active = document.querySelector('.chat__thread.is-active .chat__thread-title');
            if (active) { active.textContent = result.title; }
            App.toast('Conversation renamed.', 'success');
          })
          .catch(function (error) { App.toast(error.message, 'danger'); });
      });
    }

    var deleteButton = document.getElementById('deleteConversation');
    if (deleteButton) {
      deleteButton.addEventListener('click', function () {
        if (!window.confirm('Delete this conversation and all of its messages?')) { return; }
        App.postJSON(urls.remove, { conversation_id: conversationId })
          .then(function (result) { window.location.href = result.next_url; })
          .catch(function (error) { App.toast(error.message, 'danger'); });
      });
    }

    /* --- Copy --------------------------------------------------------------- */

    App.on('click', '[data-copy-message]', function () {
      var article = this.closest('.msg');
      var text = article.querySelector('.msg__content').textContent;
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text)
          .then(function () { App.toast('Copied to the clipboard.', 'success'); })
          .catch(function () { App.toast('Could not copy.', 'warning'); });
      } else {
        App.toast('Copying is not available in this browser.', 'warning');
      }
    });

    /* --- Send for approval --------------------------------------------------- */

    var typeSelect = document.getElementById('submitItemType');

    function syncApprovalFields() {
      App.qsa('[data-when-type]').forEach(function (field) {
        field.hidden = field.dataset.whenType !== typeSelect.value;
      });
    }
    if (typeSelect) { typeSelect.addEventListener('change', syncApprovalFields); }

    /* Pull an address out of the conversation, so asking Aria to "send to
       someone@example.com" does not then require retyping it. The most recent
       one wins, which is the one just asked about. */
    var EMAIL_IN_TEXT = /[\w.+-]+@[\w-]+\.[\w.-]+/g;

    function addressFromConversation() {
      var found = log.textContent.match(EMAIL_IN_TEXT) || [];
      return found.length ? found[found.length - 1] : '';
    }

    /* Aria replies "Subject: ...", blank line, then the body. Use that as the
       title when it is there rather than the first line of prose. The server
       splits it the same way; this only pre-fills the field. */
    function titleFromReply(content) {
      var match = content.match(/^\s*subject\s*:\s*(.+)$/im);
      if (match) { return match[1].trim().slice(0, 80); }
      return content.split('\n')[0].slice(0, 60).trim();
    }

    App.on('click', '[data-submit-message]', function () {
      var article = this.closest('.msg');
      var content = article.querySelector('.msg__content').textContent;

      document.getElementById('submitMessageId').value = this.dataset.submitMessage;
      document.getElementById('submitTitle').value = titleFromReply(content);
      document.getElementById('submitPreview').textContent = content;

      var recipient = document.getElementById('submitRecipient');
      if (recipient && !recipient.value) { recipient.value = addressFromConversation(); }

      // An email-shaped reply is almost always meant as one.
      if (typeSelect && /^\s*subject\s*:/im.test(content)) {
        typeSelect.value = 'email';
      }

      syncApprovalFields();
      App.openModal('submitApprovalModal');
    });

    /* Picking a prospect fills the address, so the two cannot disagree. */
    var leadSelect = document.getElementById('submitLead');
    if (leadSelect) {
      leadSelect.addEventListener('change', function () {
        var option = leadSelect.options[leadSelect.selectedIndex];
        var address = option ? option.dataset.email : '';
        var recipient = document.getElementById('submitRecipient');
        if (address && recipient) { recipient.value = address; }
      });
    }

    var confirmButton = document.getElementById('submitApprovalConfirm');
    if (confirmButton) {
      confirmButton.addEventListener('click', function () {
        var itemType = typeSelect.value;
        var leadId = document.getElementById('submitLead').value;
        var recipientField = document.getElementById('submitRecipient');
        var recipient = recipientField ? recipientField.value.trim() : '';

        if (itemType === 'email' && !recipient && !leadId) {
          App.toast('Enter an address, or pick a prospect, to send this to.', 'warning');
          return;
        }

        App.setLoading(confirmButton, true, 'Sending');
        App.postJSON(urls.submit, {
          message_id: document.getElementById('submitMessageId').value,
          item_type: itemType,
          title: document.getElementById('submitTitle').value,
          platform: document.getElementById('submitPlatform').value,
          lead_id: leadId || null,
          recipient_email: recipient
        })
          .then(function (result) {
            App.setLoading(confirmButton, false);
            App.closeModal('submitApprovalModal');
            App.toast(result.message, 'success');

            var badge = document.getElementById('navPendingBadge');
            if (badge) {
              badge.textContent = result.pending_count;
              badge.hidden = result.pending_count === 0;
            }

            // Replace the button with a link into the queue.
            var messageId = document.getElementById('submitMessageId').value;
            var article = document.querySelector('[data-message-id="' + messageId + '"]');
            var button = article && article.querySelector('[data-submit-message]');
            if (button) {
              var link = document.createElement('a');
              link.href = result.approval_url;
              link.className = 'btn btn--ghost btn--sm';
              link.innerHTML =
                '<i class="fa-solid fa-clipboard-check"></i> In the queue &middot; Pending Review';
              button.replaceWith(link);
            }
          })
          .catch(function (error) {
            App.setLoading(confirmButton, false);
            App.toast(error.message, 'danger');
          });
      });
    }

    /* --- Configuration panel and conversation rail --------------------------- */

    var panel = document.getElementById('agentPanel');
    var panelOverlay = document.getElementById('agentPanelOverlay');
    var panelToggle = document.getElementById('agentPanelToggle');

    function setPanel(open) {
      document.body.classList.toggle('is-panel-open', open);
      if (panel) { panel.setAttribute('aria-hidden', open ? 'false' : 'true'); }
      if (panelOverlay) { panelOverlay.setAttribute('aria-hidden', open ? 'false' : 'true'); }
      if (panelToggle) { panelToggle.setAttribute('aria-expanded', open ? 'true' : 'false'); }
      if (open && panel) {
        var first = panel.querySelector('input, textarea, select');
        if (first) { first.focus(); }
      }
    }

    if (panelToggle) {
      panelToggle.addEventListener('click', function () {
        setPanel(!document.body.classList.contains('is-panel-open'));
      });
    }
    ['agentPanelClose', 'agentPanelCancel'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.addEventListener('click', function () { setPanel(false); }); }
    });
    if (panelOverlay) { panelOverlay.addEventListener('click', function () { setPanel(false); }); }

    var railToggle = document.getElementById('chatRailToggle');
    if (railToggle) {
      railToggle.addEventListener('click', function () {
        document.body.classList.toggle('is-rail-open');
      });
    }

    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') { return; }
      setPanel(false);
      document.body.classList.remove('is-rail-open');
    });

    // A configuration form that failed validation reopens the panel.
    if (panel && panel.querySelector('.form-field__error')) { setPanel(true); }

    /* --- Start where the user left off --------------------------------------- */

    scrollToBottom();
    input.focus();
  });
})(window.App);
