"""SOClone views."""
import datetime
import itertools

from django.contrib.auth.models import User
from django.contrib.auth import views as auth_views
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator, InvalidPage
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render_to_response
from django.template import RequestContext
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe

from lxml.html.diff import htmldiff
from markdown2 import Markdown
from soclone import auth
from soclone import diff
from soclone.forms import (AddAnswerForm, AskQuestionForm, CloseQuestionForm,
    CommentForm, EditAnswerForm, EditQuestionForm, RetagQuestionForm,
    RevisionForm)
from soclone.http import JsonResponse
from soclone.models import (Answer, AnswerRevision, Badge, Comment,
    FavouriteQuestion, Question, QuestionRevision, Tag, Vote)
from soclone.questions import (all_question_views, index_question_views,
    unanswered_question_views)
from soclone.shortcuts import get_page
from soclone.utils.html import sanitize_html
from soclone.utils.models import populate_foreign_key_caches

markdowner = Markdown(html4tags=True)

AUTO_WIKI_ANSWER_COUNT = 30

def get_questions_per_page(user):
    if user.is_authenticated():
        return user.questions_per_page
    return 10

def question_list(request, question_views, template, questions_per_page=None,
                  page_number=None, extra_context=None):
    """
    Question list generic view.

    Allows the user to select from a number of ways of viewing questions,
    rendered with the given template.
    """
    view_id = request.GET.get('sort', None)
    view = dict([(q.id, q) for q in question_views]).get(view_id,
                                                         question_views[0])
    if questions_per_page is None:
        questions_per_page = get_questions_per_page(request.user)
    paginator = Paginator(view.get_queryset(), questions_per_page)
    if page_number is None:
        page = get_page(request, paginator)
    else:
        page = paginator.page(page_number)
    populate_foreign_key_caches(User, ((page.object_list, (view.user,)),),
                                fields=view.user_fields)
    context = {
        'title': view.page_title,
        'page': page,
        'questions': page.object_list,
        'current_view': view,
        'question_views': question_views,
    }
    if extra_context is not None:
        context.update(extra_context)
    return render_to_response(template, context,
                              context_instance=RequestContext(request))

def index(request):
    """A condensed version of the main Question list."""
    extra_context = {
        # TODO Retrieve extra context required for index page
    }
    return question_list(request, index_question_views, 'index.html',
                         questions_per_page=50, page_number=1,
                         extra_context=extra_context)

def about(request):
    """About SOClone."""
    raise NotImplementedError

def faq(request):
    """Frequently Asked Questions."""
    raise NotImplementedError

def search(request):
    """Search Questions and Answers."""
    raise NotImplementedError

def login(request):
    """Logs in."""
    return auth_views.login(request, template_name='login.html')

def logout(request):
    """Logs out."""
    return auth_views.logout(request, template_name='logged_out.html')

def questions(request):
    """All Questions list."""
    return question_list(request, all_question_views, 'questions.html')

def unanswered(request):
    """Unanswered Questions list."""
    return question_list(request, unanswered_question_views, 'unanswered.html')

ANSWER_SORT = {
    'votes': ('-score', '-added_at'),
    'newest': ('-added_at',),
    'oldest': ('added_at',),
}

DEFAULT_ANSWER_SORT = 'votes'

def question(request, question_id):
    """Displays a Question."""
    if not request.user.is_authenticated():
        question = get_object_or_404(Question, id=question_id)
        favourite = False
    else:
        question = get_object_or_404(Question.objects.extra(
            select={
                'user_favourite_id': (
                    'SELECT id FROM soclone_favouritequestion '
                    'WHERE question_id = soclone_question.id '
                      'AND user_id = %s'),
            },
            select_params=[request.user.id]
        ), id=question_id)
        favourite = (question.user_favourite_id is not None)

    if 'showcomments' in request.GET:
        return question_comments(request, question)

    answer_sort_type = request.GET.get('sort', DEFAULT_ANSWER_SORT)
    if answer_sort_type not in ANSWER_SORT:
        answer_sort_type = DEFAULT_ANSWER_SORT
    order_by = ANSWER_SORT[answer_sort_type]
    paginator = Paginator(Answer.objects.for_question(
                              question, request.user).order_by(*order_by),
                          AUTO_WIKI_ANSWER_COUNT)
    # Save ourselves a COUNT() query by using the denormalised count
    paginator._count = question.answer_count
    page = get_page(request, paginator)
    answers = page.object_list

    populate_foreign_key_caches(User, (
            ((question,), ('author', 'last_edited_by', 'closed_by')),
            (answers,     ('author', 'last_edited_by'))
         ),
         fields=('username', 'gravatar', 'reputation', 'gold', 'silver',
                 'bronze'))

    # Look up vote status for the current user
    question_vote, answer_votes = Vote.objects.get_for_question_and_answers(
        request.user, question, page.object_list)

    title = question.title
    if question.closed:
        title = '%s [closed]' % title
    return render_to_response('question.html', {
        'title': title,
        'question': question,
        'question_vote': question_vote,
        'favourite': favourite,
        'answers': page.object_list,
        'answer_votes': answer_votes,
        'page': page,
        'answer_sort': answer_sort_type,
        'answer_form': AddAnswerForm(),
        'tags': question.tags.all(),
    }, context_instance=RequestContext(request))

def question_comments(request, question, form=None):
    """
    Displays a Question and any Comments on it.

    This is primarily intended as a fallback for users who can't
    dynamically load Comments.
    """
    populate_foreign_key_caches(User, (
            ((question,), ('author', 'last_edited_by', 'closed_by')),
         ),
         fields=('username', 'gravatar', 'reputation', 'gold', 'silver',
                 'bronze'))

    content_type = ContentType.objects.get_for_model(Question)
    comments = Comment.objects.filter(content_type=content_type,
                                      object_id=question.id)

    if form is None:
        form = CommentForm()

    return render_to_response('question.html', {
        'title': u'Comments on %s' % question.title,
        'question': question,
        'tags': question.tags.all(),
        'comments': comments,
        'comment_form': form,
    }, context_instance=RequestContext(request))

def ask_question(request):
    """Adds a Question."""
    preview = None
    if request.method == 'POST':
        form = AskQuestionForm(request.POST)
        if form.is_valid():
            html = sanitize_html(markdowner.convert(form.cleaned_data['text']))
            if 'preview' in request.POST:
                # The user submitted the form to preview the formatted question
                preview = mark_safe(html)
            elif 'submit' in request.POST:
                added_at = datetime.datetime.now()
                # Create the Question
                question = Question(
                    title            = form.cleaned_data['title'],
                    author           = request.user,
                    added_at         = added_at,
                    wiki             = form.cleaned_data['wiki'],
                    last_activity_at = added_at,
                    last_activity_by = request.user,
                    tagnames         = form.cleaned_data['tags'],
                    html             = html,
                    summary          = strip_tags(html)[:180]
                )
                if question.wiki:
                    question.wikified_at = added_at
                    # When in wiki mode, we always display the last edit
                    question.last_edited_at = added_at
                    question.last_edited_by = request.user
                question.save()
                # Create the initial revision
                QuestionRevision.objects.create(
                    question   = question,
                    revision   = 1,
                    title      = question.title,
                    author     = request.user,
                    revised_at = added_at,
                    tagnames   = question.tagnames,
                    summary    = u'asked question',
                    text       = form.cleaned_data['text']
                )
                # TODO Badges related to Tag usage
                # TODO Badges related to asking Questions
                return HttpResponseRedirect(question.get_absolute_url())
    else:
        form = AskQuestionForm()
    return render_to_response('ask_question.html', {
        'title': u'Ask a Question',
        'form': form,
        'preview': preview,
    }, context_instance=RequestContext(request))

def edit_question(request, question_id):
    """
    Entry point for editing a question.

    Fields which can be edited depend on the logged-in user's roles or
    reputation, so this view delegates to the apporopriate view based on
    those criteria.
    """
    question = get_object_or_404(Question, id=question_id)
    if auth.can_edit_post(request.user, question):
        return _edit_question(request, question)
    elif auth.can_retag_questions(request.user):
        return _retag_question(request, question)
    else:
        raise Http404

def _edit_question(request, question):
    """
    Allows the user to edit a Question's title, text and tags.

    If the Question is not already in wiki mode, the user can put it in
    wiki mode, or it will automatically be put in wiki mode if the
    question has been edited five times by the person who asked it, or
    has been edited by four different users.
    """
    latest_revision = question.get_latest_revision()
    preview = None
    revision_form = None
    if request.method == 'POST':
        if 'select_revision' in request.POST:
            # The user submitted to change the revision to start editing from
            revision_form = RevisionForm(question, latest_revision, request.POST)
            if revision_form.is_valid():
                # Replace Question details with those from the selected revision
                form = EditQuestionForm(question,
                    QuestionRevision.objects.get(question=question,
                        revision=revision_form.cleaned_data['revision']))
            else:
                # Make sure we keep a hold of the user's other input, even
                # though they appear to be messing about.
                form = EditQuestionForm(question, latest_revision, request.POST)
        else:
            # Always check modifications against the latest revision
            form = EditQuestionForm(question, latest_revision, request.POST)
            if form.is_valid():
                html = sanitize_html(
                    markdowner.convert(form.cleaned_data['text']))
                if 'preview' in request.POST:
                    # The user submitted to preview the formatted question
                    preview = mark_safe(html)
                elif 'submit' in request.POST:
                    if form.has_changed():
                        edited_at = datetime.datetime.now()
                        tags_changed = (latest_revision.tagnames !=
                                        form.cleaned_data['tags'])
                        tags_updated = False
                        # Update the Question itself
                        updated_fields = {
                            'title': form.cleaned_data['title'],
                            'last_edited_at': edited_at,
                            'last_edited_by': request.user,
                            'last_activity_at': edited_at,
                            'last_activity_by': request.user,
                            'tagnames': form.cleaned_data['tags'],
                            'summary': strip_tags(html)[:180],
                            'html': html,
                        }
                        if ('wiki' in form.cleaned_data and
                            form.cleaned_data['wiki']):
                            updated_fields['wiki'] = True
                            updated_fields['wikified_at'] = edited_at
                        Question.objects.filter(
                            id=question.id).update(**updated_fields)
                        # Update the Question's tag associations
                        if tags_changed:
                            tags_updated = Question.objects.update_tags(
                                question, question.tagnames, request.user)
                        # Create a new revision
                        revision = QuestionRevision(
                            question   = question,
                            title      = form.cleaned_data['title'],
                            author     = request.user,
                            revised_at = edited_at,
                            tagnames   = form.cleaned_data['tags'],
                            text       = form.cleaned_data['text']
                        )
                        if form.cleaned_data['summary']:
                            revision.summary = form.cleaned_data['summary']
                        else:
                            revision.summary = \
                                diff.generate_question_revision_summary(
                                    latest_revision, revision,
                                    ('wiki' in updated_fields))
                        revision.save()
                        # TODO 5 body edits by the author = automatic wiki mode
                        # TODO 4 individual editors = automatic wiki mode
                        # TODO Badges related to Tag usage
                        # TODO Badges related to editing Questions
                    return HttpResponseRedirect(question.get_absolute_url())
    else:
        if 'revision' in request.GET:
            revision_form = RevisionForm(question, latest_revision, request.GET)
            if revision_form.is_valid():
                # Replace Question details with those from the selected revision
                form = EditQuestionForm(question,
                    QuestionRevision.objects.get(question=question,
                        revision=revision_form.cleaned_data['revision']))
        else:
            revision_form = RevisionForm(question, latest_revision)
            form = EditQuestionForm(question, latest_revision)
    if revision_form is None:
        # We're about to redisplay after a POST where we didn't care which
        # revision was selected - make sure the revision the user started from
        # is still selected on redisplay.
        revision_form = RevisionForm(question, latest_revision, request.POST)
    return render_to_response('edit_question.html', {
        'title': u'Edit Question',
        'question': question,
        'revision_form': revision_form,
        'form': form,
        'preview': preview,
    }, context_instance=RequestContext(request))

def _retag_question(request, question):
    """Allows the user to edit a Question's tags."""
    if request.method == 'POST':
        form = RetagQuestionForm(question, request.POST)
        if form.is_valid():
            if form.has_changed():
                latest_revision = question.get_latest_revision()
                retagged_at = datetime.datetime.now()
                # Update the Question itself
                Question.objects.filter(id=question.id).update(
                    tagnames         = form.cleaned_data['tags'],
                    last_edited_at   = retagged_at,
                    last_edited_by   = request.user,
                    last_activity_at = retagged_at,
                    last_activity_by = request.user
                )
                # Update the Question's tag associations
                tags_updated = Question.objects.update_tags(question,
                    form.cleaned_data['tags'], request.user)
                # Create a new revision
                QuestionRevision.objects.create(
                    question   = question,
                    title      = latest_revision.title,
                    author     = request.user,
                    revised_at = retagged_at,
                    tagnames   = form.cleaned_data['tags'],
                    summary    = u'modified tags',
                    text       = latest_revision.text
                )
                # TODO Badges related to retagging / Tag usage
                # TODO Badges related to editing Questions
            return HttpResponseRedirect(question.get_absolute_url())
    else:
        form = RetagQuestionForm(question)
    return render_to_response('retag_question.html', {
        'title': u'Edit Tags',
        'question': question,
        'form': form,
    }, context_instance=RequestContext(request))

QUESTION_REVISION_TEMPLATE = ('<h1>%(title)s</h1>\n'
    '<div class="text">%(html)s</div>\n'
    '<div class="tags">%(tags)s</div>')

def question_revisions(request, question_id):
    """Revision history for a Question."""
    question = get_object_or_404(Question, id=question_id)
    revisions = list(question.revisions.all())
    populate_foreign_key_caches(User, ((revisions, ('author',)),),
         fields=('username', 'gravatar', 'reputation', 'gold', 'silver',
                 'bronze'))
    for i, revision in enumerate(revisions):
        revision.html = QUESTION_REVISION_TEMPLATE % {
            'title': revision.title,
            'html': sanitize_html(markdowner.convert(revision.text)),
            'tags': ' '.join(['<a class="tag">%s</a>' % tag
                              for tag in revision.tagnames.split(' ')]),
        }
        if i > 0:
            revisions[i - 1].diff = htmldiff(revision.html,
                                             revisions[i - 1].html)
    return render_to_response('question_revisions.html', {
        'title': u'Question Revisions',
        'question': question,
        'revisions': revisions,
    }, context_instance=RequestContext(request))

def close_question(request, question_id):
    """Closes or reopens a Question based on its current closed status."""
    question = get_object_or_404(Question, id=question_id)
    if not auth.can_close_question(request.user, question):
        raise Http404
    if not question.closed:
        return _close_question(request, question)
    else:
        return _reopen_question(request, question)

def _close_question(request, question):
    """Closes a Question."""
    if request.method == 'POST' and 'close' in request.POST:
        form = CloseQuestionForm(request.POST)
        if form.is_valid():
            Question.objects.filter(id=question.id).update(closed=True,
                closed_by=request.user, closed_at=datetime.datetime.now(),
                close_reason=form.cleaned_data['reason'])
            if request.is_ajax():
                return JsonResponse({'success': True})
            else:
                return HttpResponseRedirect(question.get_absolute_url())
        elif request.is_ajax():
            return JsonResponse({'success': False, 'errors': form.errors})
    else:
        if request.is_ajax():
            raise Http404
        form = CloseQuestionForm()
    return render_to_response('close_question.html', {
        'title': u'Close Question',
        'question': question,
        'form': form,
    }, context_instance=RequestContext(request))

def _reopen_question(request, question):
    """Reopens a Question."""
    if request.method == 'POST' and 'reopen' in request.POST:
        Question.objects.filter(id=question.id).update(closed=False,
            closed_by=None, closed_at=None, close_reason=None)
        if request.is_ajax():
            return JsonResponse({'success': True})
        else:
            return HttpResponseRedirect(question.get_absolute_url())
    if request.is_ajax():
        raise Http404
    return render_to_response('reopen_question.html', {
        'title': u'Reopen Question',
        'question': question,
    }, context_instance=RequestContext(request))

def delete_question(request, question_id):
    """Deletes or undeletes a Question."""
    raise NotImplementedError

def favourite_question(request, question_id):
    """
    Adds or removes a FavouriteQuestion.

    Favouriting will not use a confirmation page, as it's an action which
    is non-destructive and easily reversible.
    """
    if request.method != 'POST':
        raise Http404

    question = get_object_or_404(Question, id=question_id, deleted=False)
    favourite, created = FavouriteQuestion.objects.get_or_create(
        user=request.user, question=question)
    if not created:
        favourite.delete()

    if request.is_ajax():
        return JsonResponse({'success': True, 'favourited': created})
    else:
        return HttpResponseRedirect(question.get_absolute_url())

def add_answer(request, question_id):
    """
    Adds an Answer to a Question.

    Once a certain number of Answers have been added, a Question and all
    its Answers will enter wiki mode and all subsequent Answers will be in
    wiki mode.
    """
    question = get_object_or_404(Question, id=question_id)
    preview = None
    if request.method == 'POST':
        form = AddAnswerForm(request.POST)
        if form.is_valid():
            html = sanitize_html(markdowner.convert(form.cleaned_data['text']))
            if 'preview' in request.POST:
                # The user submitted the form to preview the formatted answer
                preview = mark_safe(html)
            elif 'submit' in request.POST:
                added_at = datetime.datetime.now()
                # Create the Answer
                answer = Answer(
                    question = question,
                    author   = request.user,
                    added_at = added_at,
                    wiki     = (form.cleaned_data['wiki'] or
                                question.answer_count >= AUTO_WIKI_ANSWER_COUNT),
                    html     = html
                )
                if answer.wiki:
                    answer.wikified_at = added_at
                    # When in wiki mode, we always display the last edit
                    answer.last_edited_at = added_at
                    answer.last_edited_by = request.user
                answer.save()
                # Create the initial revision
                AnswerRevision.objects.create(
                    answer     = answer,
                    revision   = 1,
                    author     = request.user,
                    revised_at = added_at,
                    summary    = u'added answer',
                    text       = form.cleaned_data['text']
                )
                Question.objects.update_answer_count(question)
                # TODO Badges related to answering Questions
                # TODO If this is answer 30, put question and all answers into
                #      wiki mode.
                # TODO Redirect needs to handle paging
                return HttpResponseRedirect(question.get_absolute_url())
    else:
        form = AddAnswerForm()
    return render_to_response('add_answer.html', {
        'title': u'Post an Answer',
        'question': question,
        'form': form,
        'preview': preview,
    }, context_instance=RequestContext(request))

def answer_comments(request, answer_id, answer=None, form=None):
    """
    Displays a single Answer and any Comments on it.

    This is primarily intended as a fallback for users who can't
    dynamically load Comments.
    """
    if answer is None:
        answer = get_object_or_404(Answer, id=answer_id)

    populate_foreign_key_caches(User, (
            ((answer,), ('author', 'last_edited_by')),
         ),
         fields=('username', 'gravatar', 'reputation', 'gold', 'silver',
                 'bronze'))

    content_type = ContentType.objects.get_for_model(Answer)
    comments = Comment.objects.filter(content_type=content_type,
                                      object_id=answer.id)
    if form is None:
        form = CommentForm()

    return render_to_response('answer.html', {
        'title': u'Answer Comments',
        'answer': answer,
        'comments': comments,
        'comment_form': form,
    }, context_instance=RequestContext(request))

def edit_answer(request, answer_id):
    """Edits an Answer."""
    answer = get_object_or_404(Answer, id=answer_id)
    if not auth.can_edit_post(request.user, answer):
        raise Http404
    latest_revision = answer.get_latest_revision()
    preview = None
    revision_form = None
    if request.method == 'POST':
        if 'select_revision' in request.POST:
            # The user submitted to change the revision to start editing from
            revision_form = RevisionForm(answer, latest_revision, request.POST)
            if revision_form.is_valid():
                # Replace Question details with those from the selected revision
                form = EditAnswerForm(answer,
                    AnswerRevision.objects.get(answer=answer,
                        revision=revision_form.cleaned_data['revision']))
            else:
                # Make sure we keep a hold of the user's other input, even
                # though they appear to be messing about.
                form = EditAnswerForm(answer, latest_revision, request.POST)
        else:
            # Always check modifications against the latest revision
            form = EditAnswerForm(answer, latest_revision, request.POST)
            if form.is_valid():
                html = sanitize_html(
                    markdowner.convert(form.cleaned_data['text']))
                if 'preview' in request.POST:
                    # The user submitted to preview the formatted question
                    preview = mark_safe(html)
                elif 'submit' in request.POST:
                    if form.has_changed():
                        edited_at = datetime.datetime.now()
                        # Update the Answer itself
                        updated_fields = {
                            'last_edited_at': edited_at,
                            'last_edited_by': request.user,
                            'html': html,
                        }
                        if ('wiki' in form.cleaned_data and
                            form.cleaned_data['wiki']):
                            updated_fields['wiki'] = True
                            updated_fields['wikified_at'] = edited_at
                        Answer.objects.filter(
                            id=answer.id).update(**updated_fields)
                        # Create a new revision
                        revision = AnswerRevision(
                            answer = answer,
                            author = request.user,
                            revised_at = edited_at,
                            text = form.cleaned_data['text']
                        )
                        if form.cleaned_data['summary']:
                            revision.summary = form.cleaned_data['summary']
                        else:
                            revision.summary = \
                                diff.generate_answer_revision_summary(
                                    latest_revision, revision,
                                    ('wiki' in updated_fields))
                        revision.save()
                        # TODO 5 body edits by the asker = automatic wiki mode
                        # TODO 4 individual editors = automatic wiki mode
                        # TODO Badges related to editing Answers
                    return HttpResponseRedirect(answer.get_absolute_url())
    else:
        revision_form = RevisionForm(answer, latest_revision)
        form = EditAnswerForm(answer, latest_revision)
    if revision_form is None:
        # We're about to redisplay after a POST where we didn't care which
        # revision was selected - make sure the revision the user started from
        # is still selected on redisplay.
        revision_form = RevisionForm(answer, latest_revision, request.POST)
    return render_to_response('edit_answer.html', {
        'title': u'Edit Answer',
        'question': answer.question,
        'answer': answer,
        'revision_form': revision_form,
        'form': form,
        'preview': preview,
    }, context_instance=RequestContext(request))

ANSWER_REVISION_TEMPLATE = '<div class="text">%(html)s</div>'

def answer_revisions(request, answer_id):
    """Revision history for an Answer."""
    answer = get_object_or_404(Answer, id=answer_id)
    revisions = list(answer.revisions.all())
    populate_foreign_key_caches(User, ((revisions, ('author',)),),
         fields=('username', 'gravatar', 'reputation', 'gold', 'silver',
                 'bronze'))
    for i, revision in enumerate(revisions):
        revision.html = QUESTION_REVISION_TEMPLATE % {
            'html': sanitize_html(markdowner.convert(revision.text)),
        }
        if i > 0:
            revisions[i - 1].diff = htmldiff(revision.html,
                                             revisions[i - 1].html)
    return render_to_response('answer_revisions.html', {
        'title': u'Answer Revisions',
        'answer': answer,
        'revisions': revisions,
    }, context_instance=RequestContext(request))

def accept_answer(request, answer_id):
    """Marks an Answer as accepted."""
    raise NotImplementedError

def delete_answer(request, answer_id):
    """Deletes or undeletes an Answer."""
    raise NotImplementedError

def vote(request, model, object_id):
    """
    Vote on a Question or Answer.
    """
    if request.method != 'POST':
        raise Http404

    vote_type = request.POST.get('type', None)
    if vote_type == 'up' and auth.can_vote_up(request.user):
        vote_type = Vote.VOTE_UP
    elif vote_type == 'down' and auth.can_vote_down(request.user):
        vote_type = Vote.VOTE_DOWN
    else:
        raise Http404

    # TODO Ensure users can't vote on their own posts

    obj = get_object_or_404(model, id=object_id, deleted=False, locked=False)
    content_type = ContentType.objects.get_for_model(model)
    try:
        existing_vote = Vote.objects.get(content_type=content_type,
                                         object_id=object_id,
                                         user=request.user)
    except Vote.DoesNotExist:
        existing_vote = None

    if existing_vote is None:
        Vote.objects.create(content_type=content_type,
                            object_id=object_id,
                            user=request.user,
                            vote=vote_type)
    else:
        if vote_type == existing_vote.vote:
            existing_vote.delete()
        else:
            existing_vote.vote = vote_type
            existing_vote.save()

    # TODO Reputation management

    if request.is_ajax():
        return JsonResponse({
            'success': True,
            'score': model._default_manager.filter(
                id=object_id).values_list('score', flat=True)[0],
        })
    else:
        return HttpResponseRedirect(obj.get_absolute_url())

def flag_item(request, model, object_id):
    """Flag a Question or Answer as containing offensive content."""
    raise NotImplementedError

def add_comment(request, model, object_id):
    """Adds a comment to a Question or Answer."""
    obj = get_object_or_404(model, id=object_id)
    if request.method == "POST":
        form = CommentForm(request.POST)
        if form.is_valid():
            Comment.objects.create(
                content_type = ContentType.objects.get_for_model(model),
                object_id    = object_id,
                author       = request.user,
                added_at     = datetime.datetime.now(),
                comment      = form.cleaned_data['comment']
            )
            if request.is_ajax():
                return JsonResponse({'success': True})
            else:
                return HttpResponseRedirect(obj.get_absolute_url())
        elif request.is_ajax():
            return JsonResponse({'success': False, 'errors': form.errors})
    else:
        form = CommentForm()

    # Let the appropriate fallback view take care of display/redisplay
    if model is Question:
        return question_comments(request, obj, form=form)
    elif model is Answer:
        return answer_comments(request, object_id, answer=obj, form=form)

def delete_comment(request, comment_id):
    """Deletes a Comment permenantly."""
    raise NotImplementedError

TAG_SORT = {
    'popular': ('-use_count', 'name'),
    'name': ('name',),
}

DEFAULT_TAG_SORT = 'popular'

def tags(request):
    """Searchable Tag list."""
    sort_type = request.GET.get('sort', DEFAULT_TAG_SORT)
    if sort_type not in TAG_SORT:
        sort_type = DEFAULT_TAG_SORT
    tags = Tag.objects.all().order_by(*TAG_SORT[sort_type])
    name_filter = request.GET.get('filter', '')
    if name_filter:
        tags = tags.filter(name__icontains=name_filter)
    paginator = Paginator(tags, 50)
    page = get_page(request, paginator)
    return render_to_response('tags.html', {
        'title': u'Tags',
        'tags': page.object_list,
        'page': page,
        'sort': sort_type,
        'filter': name_filter,
    }, context_instance=RequestContext(request))

def tag(request, tag_name):
    """Displayed Questions for a Tag."""
    raise NotImplementedError

USER_SORT = {
    'reputation': ('-reputation', '-date_joined'),
    'newest': ('-date_joined',),
    'oldest': ('date_joined',),
    'name': ('username',),
}

DEFAULT_USER_SORT = 'reputation'

def users(request):
    """Searchable User list."""
    sort_type = request.GET.get('sort', DEFAULT_USER_SORT)
    if sort_type not in USER_SORT:
        sort_type = DEFAULT_USER_SORT
    users = User.objects.all().order_by(*USER_SORT[sort_type])
    name_filter = request.GET.get('filter', '')
    if name_filter:
        users = users.filter(username__icontains=name_filter)
    users = users.values('id', 'username', 'gravatar',  'reputation', 'gold',
                         'silver', 'bronze')
    paginator = Paginator(users, 28)
    page = get_page(request, paginator)
    return render_to_response('users.html', {
        'title': u'Users',
        'users': page.object_list,
        'page': page,
        'sort': sort_type,
        'filter': name_filter,
    }, context_instance=RequestContext(request))

def user(request, user_id):
    """Displays a User and various information about them."""
    raise NotImplementedError

def badges(request):
    """Badge list."""
    return render_to_response('badges.html', {
        'title': u'Badges',
        'badges': Badge.objects.all(),
    }, context_instance=RequestContext(request))

def badge(request, badge_id):
    """Displays a Badge and any Users who have recently been awarded it."""
    badge = get_object_or_404(Badge, id=badge_id)
    awarded_to = badge.awarded_to.all().order_by('-award__awarded_at').values(
        'id', 'username', 'reputation', 'gold', 'silver', 'bronze')[:500]
    return render_to_response('badge.html', {
        'title': '%s Badge' % badge.name,
        'badge': badge,
        'awarded_to': awarded_to,
    }, context_instance=RequestContext(request))
