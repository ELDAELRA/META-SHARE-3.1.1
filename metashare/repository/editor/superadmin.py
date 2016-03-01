'''
Custom base classes for admin interface, for both the top-level admin page
and for inline forms.
'''
import logging

from django import template
from django.conf import settings
from django.contrib import admin
from django.contrib.admin import helpers
from django.contrib.admin.utils import unquote, get_deleted_objects
from django.core.exceptions import PermissionDenied
from django.db import transaction, models, router
from django.forms.formsets import all_valid
from django.http import Http404, HttpResponseRedirect, HttpResponse
from django.shortcuts import render_to_response
from django.utils.decorators import method_decorator
from django.utils.encoding import force_unicode
from django.utils.html import escape, escapejs
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext as _
from django.views.decorators.csrf import csrf_protect

from metashare.repository import model_utils
from metashare.repository.editor.editorutils import is_inline, decode_inline
from metashare.repository.editor.inlines import ReverseInlineModelAdmin
from metashare.repository.editor.related_mixin import RelatedAdminMixin
from metashare.repository.editor.schemamodel_mixin import SchemaModelLookup
from metashare.storage.models import MASTER
from metashare.repository.model_utils import get_root_resources
from metashare.repository.editor.editorutils import MetaShareSearchModelAdmin
from metashare.repository.supermodel import REQUIRED, RECOMMENDED, OPTIONAL

from django.contrib.admin.exceptions import DisallowedModelAdminToField
from django.utils.encoding import force_text
from django.core.urlresolvers import reverse
from django.contrib.contenttypes.models import ContentType

IS_POPUP_VAR = '_popup'
TO_FIELD_VAR = '_to_field'

# Setup logging support.
LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(settings.LOG_HANDLER)

csrf_protect_m = method_decorator(csrf_protect)


class SchemaModelAdmin(MetaShareSearchModelAdmin, RelatedAdminMixin, SchemaModelLookup):
    '''
    Patched ModelAdmin class. The add_view method is overridden to
    allow the reverse inline formsets to be saved before the parent
    model.
    '''    
    custom_one2one_inlines = {}
    custom_one2many_inlines = {}
    inline_type = 'stacked'
    inlines = ()
    
    class Media:
        js = (settings.MEDIA_URL + 'js/addCollapseToAllStackedInlines.js',
              settings.MEDIA_URL + 'js/jquery-ui.min.js',
              settings.MEDIA_URL + 'js/help.js',
              settings.ADMIN_MEDIA_PREFIX + 'js/collapse.min.js',)
        css = {'all': (settings.ADMIN_MEDIA_PREFIX + 'css/themes/smoothness/jquery-ui.css',)}

    def __init__(self, model, admin_site):
        # Get from the model all inlines grouped by Required/Recommended/Optional status:
        self.inlines += tuple(self.get_inline_classes(model, status=REQUIRED) + \
          self.get_inline_classes(model, status=RECOMMENDED) + \
          self.get_inline_classes(model, status=OPTIONAL))
        # Show m2m fields as horizontal filter widget unless they have a custom widget:
        self.filter_horizontal = self.list_m2m_fields_without_custom_widget(model)
        admin_site.site_url = "/"
        super(SchemaModelAdmin, self).__init__(model, admin_site)
        # Reverse inline code:
        self.no_inlines = []
        self.inline_instances = []
        self.exclude = self.exclude or []
        if not isinstance(self.exclude, list):
            self.exclude = list(self.exclude)
        # Prepare inlines for the required one2one fields:
        for field in model._meta.fields:
            if isinstance(field, models.OneToOneField):
                name = field.name
                if not name in self.no_inlines and not name in self.exclude and not name in self.readonly_fields:
                    if self.contains_inlines(field.rel.to):
                        # ignore fields referring to models that "contain"
                        # inlines; if we wouldn't ignore these, these
                        # inlines would simply not show up because of the
                        # internal nested structure
                        self.no_inlines.append(name)
                        continue
                    parent = field.related.parent_model
                    if name == '{}_ptr'.format(parent.__name__.lower()):
                        # ignore fields generated by Django because of model
                        # inheritance (?)
                        self.no_inlines.append(name)
                        continue
                    _inline_class = ReverseInlineModelAdmin
                    if name in self.custom_one2one_inlines:
                        _inline_class = self.custom_one2one_inlines[name]
                    inline = _inline_class(model,
                                           name,
                                           parent,
                                           admin_site,
                                           self.inline_type)
                    self.inline_instances.append(inline)
                    self.exclude.append(name)


    def get_actions(self, request):
        """
        Return a dictionary mapping the names of all actions for this
        `ModelAdmin` to a tuple of (callable, name, description) for each
        action.
        """
        result = super(SchemaModelAdmin, self).get_actions(request)
        if not request.user.is_superuser:
            # only superusers can see the delete action; this makes sense as
            # currently deleting would mostly not work anyway due to related
            # objects which can't be deleted
            del result['delete_selected']
        return result


    def contains_inlines(self, model_class):
        ''' Determine whether or not the editor for the given model_class will contain inlines '''
        return any(f for f in model_class.get_fields_flat() if f.endswith('_set'))


    def get_fieldsets(self, request, obj=None):
        return SchemaModelLookup.get_fieldsets(self, request, obj)


    def formfield_for_dbfield(self, db_field, **kwargs):
        """
        This is a crucial step in the workflow: for a given db field,
        it is decided how this field will be rendered in the form.
        We have heavily customized this; implementations are in
        RelatedAdminMixin.
        
        Customizations include:
        - hiding certain fields (they are present but invisible);
        - custom widgets for subclassable items such as actorInfo;
        - custom minimalistic "related" widget for non-inlined one2one fields;
        """
        self.hide_hidden_fields(db_field, kwargs)
        # ForeignKey or ManyToManyFields
        if self.is_x_to_many_relation(db_field):
            return self.formfield_for_relation(db_field, **kwargs)
        self.use_hidden_widget_for_one2one(db_field, kwargs)
        lang_widget = self.add_lang_widget(db_field)
        kwargs.update(lang_widget)
        formfield = super(SchemaModelAdmin, self).formfield_for_dbfield(db_field, **kwargs)
        self.use_related_widget_where_appropriate(db_field, kwargs, formfield)
        return formfield


    def has_change_permission(self, request, obj=None):
        result = super(SchemaModelAdmin, self) \
            .has_change_permission(request, obj)
        if result and obj:
            if request.user.is_superuser:
                return True
            # find out to which resourceInfoType_model instance the obj belongs
            root_resources = get_root_resources(obj)
            if len(root_resources) == 0:
                # some model instances are created before the (future) root
                # resource is actually saved, e.g., toolServiceInfo; in this
                # case the user is probably in the process of editing this model
                # instance and therefore we have to allow her to change it
                return True
            # in addition to the default change permission determination, we
            # only allow a user to edit a model if she is either owner or an
            # authorized editor of the resource to which the model belongs
            usr_grp_names = request.user.groups.values_list('name', flat=True)
            for res in root_resources:
                if request.user in res.owners.all() \
                        or any(res_group.name in usr_grp_names
                               for res_group in res.editor_groups.all()):
                    return True
            return False
        return result


    def response_add(self, request, obj, post_url_continue='../%s/'):
        if '_popup' in request.REQUEST:
            if '_subclass' in request.REQUEST:
                pk_value = obj._get_pk_val()
                class_name = obj.__class__.__name__.lower()
                return HttpResponse('<script type="text/javascript">opener.dismissAddAnotherPopup(window, "%s", "%s", "%s");</script>' % \
                    # escape() calls force_unicode.
                    (escape(pk_value), escapejs(obj), escapejs(class_name)))
        return super(SchemaModelAdmin, self).response_add(request, obj, post_url_continue)
    
    def response_change(self, request, obj):
        '''
        Response sent after a successful submission of a change form.
        We customize this to allow closing edit popups in the same way
        as response_add deals with add popups.
        '''
        if '_popup_o2m' in request.REQUEST:
            caller = None
            if '_caller' in request.REQUEST:
                caller = request.REQUEST['_caller']
            return self.edit_response_close_popup_magic_o2m(obj, caller)
        elif '_popup' in request.REQUEST:
            if request.POST.has_key("_continue"):
                return self.save_and_continue_in_popup(obj, request)
            return self.edit_response_close_popup_magic(obj)
        else:
            return super(SchemaModelAdmin, self).response_change(request, obj)


    def response_delete(self, request):
        '''
        Response sent after a successful deletion.
        '''
        if '_popup' in request.REQUEST:
            return HttpResponse('<script type="text/javascript">opener.dismissDeleteRelatedPopup(window);</script>')
        if not self.has_change_permission(request, None):
            return HttpResponseRedirect("../../../../")
        return HttpResponseRedirect("../../")


    def set_required_formset(self, formset):
        req_forms = formset.forms
        for req_form in req_forms:
            if not 'DELETE' in req_form.changed_data:
                req_form.empty_permitted = False
                break
            
    def get_formsets(self, request, obj=None):
        for inline in self.inline_instances:
            yield inline.get_formset(request, obj)
            
    @csrf_protect_m
    @transaction.atomic
    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
    
        to_field = request.POST.get(TO_FIELD_VAR, request.GET.get(TO_FIELD_VAR))
        if to_field and not self.to_field_allowed(request, to_field):
            raise DisallowedModelAdminToField("The field %s cannot be referenced." % to_field)
    
        model = self.model
        opts = model._meta
        add = object_id is None
    
        if add:
            if not self.has_add_permission(request):
                raise PermissionDenied
            obj = None
    
        else:
            obj = self.get_object(request, unquote(object_id))
    
            if not self.has_change_permission(request, obj):
                raise PermissionDenied
    
            if obj is None:
                raise Http404(_('%(name)s object with primary key %(key)r does not exist.') % {
                    'name': force_text(opts.verbose_name), 'key': escape(object_id)})
    
            if request.method == 'POST' and "_saveasnew" in request.POST:
                return self.add_view(request, form_url=reverse('admin:%s_%s_add' % (
                    opts.app_label, opts.model_name),
                    current_app=self.admin_site.name))
                    
        request.session.set_expiry(settings.SESSION_COOKIE_AGE)
        
        ModelForm = self.get_form(request, obj)
        formsets = []
        if request.method == 'POST':
            form = ModelForm(request.POST, request.FILES, instance=obj)
            if form.is_valid():
                form_validated = True
                new_object = self.save_form(request, form, change=not add)
            else:
                form_validated = False
                new_object = form.instance
                
            prefixes = {}
            
            for FormSet, inline in zip(self.get_formsets(request, obj), self.inline_instances):
                #### begin modification ####
                if getattr(FormSet, 'parent_fk_name', None) in self.no_inlines:
                    continue
                #### end modification ####
                prefix = FormSet.get_default_prefix()
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
                if prefixes[prefix] != 1:
                    prefix = "%s-%s" % (prefix, prefixes[prefix])
                formset = FormSet(data=request.POST, files=request.FILES,
                                  instance=new_object,
                                  save_as_new="_saveasnew" in request.POST,
                                  prefix=prefix, queryset=inline.queryset(request))
                #### begin modification ####
                if prefix in self.model.get_fields()['required']:
                    self.set_required_formset(formset)
                #### end modification ####
                formsets.append(formset)
            
            if all_valid(formsets) and form_validated:
                #### begin modification ####
                unsaved_formsets = []
                for formset in formsets:
                    parent_fk_name = getattr(formset, 'parent_fk_name', '')
                    # for the moment ignore all formsets that are no reverse
                    # inlines
                    if not parent_fk_name:
                        unsaved_formsets.append(formset)
                        continue
                    # this replaces the call to self.save_formsets()
                    changes = formset.save()
                    # if the current reverse inline is used (i.e., filled with
                    # data), then we need to manually make sure that the inline
                    # data is connected to the parent object:
                    if changes:
                        assert len(changes) == 1
                        setattr(new_object, parent_fk_name, changes[0])
                self.save_model(request, new_object, form, not add)
                self.save_related(request, form, formsets, not add)   
                for formset in unsaved_formsets:
                    self.save_formset(request, form, formset, change=False)
                # for resource info, explicitly write its metadata XML and
                # storage object to the storage folder
                if self.model.__schema_name__ == "resourceInfo":
                    new_object.storage_object.update_storage()
                #### end modification ####
                if add:
                    self.log_addition(request, new_object)
                    return self.response_add(request, new_object)
                else:
                    change_message = self.construct_change_message(request, form, formsets)
                    self.log_change(request, new_object, change_message)
                    return self.response_change(request, new_object)
        else:
            if add:
                # Prepare the dict of initial data from the request.
                # We have to special-case M2Ms as a list of comma-separated PKs.
                initial = dict(request.GET.items())
                for k in initial:
                    try:
                        f = opts.get_field(k)
                    except models.FieldDoesNotExist:
                        continue
                    if isinstance(f, models.ManyToManyField):
                        initial[k] = initial[k].split(",")
                form = ModelForm(initial=initial)
                prefixes = {}
                for FormSet, inline in zip(self.get_formsets(request),
                                           self.inline_instances):
                    #### begin modification ####
                    if getattr(FormSet, 'parent_fk_name', None) in self.no_inlines:
                        continue
                    #### end modification ####
                    prefix = FormSet.get_default_prefix()
                    prefixes[prefix] = prefixes.get(prefix, 0) + 1
                    if prefixes[prefix] != 1:
                        prefix = "%s-%s" % (prefix, prefixes[prefix])
                    formset = FormSet(instance=self.model(), prefix=prefix,
                                      queryset=inline.queryset(request))
                    formsets.append(formset)
            else: #TODO, change_view
                form = ModelForm(instance=obj)
                formsets, inline_instances = self._create_formsets(request, obj, change=True)
                
        #### begin modification ####
        media = self.media or []
        #### end modification ####
        inline_admin_formsets = []
        prepopulated = {}
        for inline, formset in zip(self.inline_instances, formsets):
            fieldsets = list(inline.get_fieldsets(request))
            readonly = list(inline.get_readonly_fields(request))
            prepopulated.update(dict(inline.get_prepopulated_fields(request, obj)))
            inline_admin_formset = helpers.InlineAdminFormSet(inline, formset,
                fieldsets, prepopulated, readonly, model_admin=self)
            #### begin modification ####
            self.add_lang_templ_params(inline_admin_formset)
            #### end modification ####
            inline_admin_formsets.append(inline_admin_formset)
            media = media + inline_admin_formset.media
            
        adminForm = OrderedAdminForm(form, list(self.get_fieldsets_with_inlines(request, obj)),
            prepopulated, self.get_readonly_fields(request),
            model_admin=self, inlines=inline_admin_formsets)
        
        media = self.media + adminForm.media
        context = dict(self.admin_site.each_context(),
            title=(_('Add %s') if add else _('Change %s')) % force_text(opts.verbose_name),
            adminform=adminForm,
            object_id=object_id,
            original=obj,
            is_popup=(IS_POPUP_VAR in request.POST or
                      IS_POPUP_VAR in request.GET),
            to_field=to_field,
            media=media,
            inline_admin_formsets=inline_admin_formsets,
            errors=helpers.AdminErrorList(form, formsets),
            preserved_filters=self.get_preserved_filters(request),
            kb_link=settings.KNOWLEDGE_BASE_URL,
            root_path=self.admin_site.site_url,
            app_label=opts.app_label,
            show_delete=False,
        )
    
        context.update(extra_context or {})
        return self.render_change_form(request, context, add=add, change=not add, obj=obj, form_url=form_url)
    
    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        opts = self.model._meta
        app_label = opts.app_label
        context.update({
            'add': add,
            'change': change,
            'has_add_permission': self.has_add_permission(request),
            'has_change_permission': self.has_change_permission(request, obj),
            'has_delete_permission': self.has_delete_permission(request, obj),
            'has_file_field': True, # FIXME - this should check if form or formsets have a FileField,
            'has_absolute_url': hasattr(self.model, 'get_absolute_url'),
            'form_url': mark_safe(form_url),
            'opts': opts,
            'content_type_id': ContentType.objects.get_for_model(self.model).id,
            'save_as': self.save_as,
            'save_on_top': self.save_on_top,
            'root_path': self.admin_site.site_url,
        })
        if add and self.add_form_template is not None:
            form_template = self.add_form_template
        else:
            form_template = self.change_form_template
        context_instance = template.RequestContext(request, current_app=self.admin_site.name)
        return render_to_response(form_template or [
            "admin/%s/%s/change_form.html" % (app_label, opts.object_name.lower()),
            "admin/%s/change_form.html" % app_label,
            "admin/change_form.html"
        ], context, context_instance=context_instance)
        
    @csrf_protect_m
    @transaction.atomic
    def delete_view(self, request, object_id, extra_context=None):
        """
        The 'delete' admin view for this model.
        This follows closely the base implementation from Django 1.3's
        django.contrib.admin.options.ModelAdmin,
        with the explicitly marked modifications.
        """
        opts = self.model._meta
        app_label = opts.app_label

        obj = self.get_object(request, unquote(object_id))

        if not self.has_delete_permission(request, obj):
            raise PermissionDenied

        if obj is None:
            raise Http404(_('%(name)s object with primary key %(key)r does not exist.') % {'name': force_unicode(opts.verbose_name), 'key': escape(object_id)})

        using = router.db_for_write(self.model)

        # Populate deleted_objects, a data structure of all related objects that
        # will also be deleted.
        (deleted_objects, perms_needed, protected) = get_deleted_objects(
            [obj], opts, request.user, self.admin_site, using)

        if request.POST: # The user has already confirmed the deletion.
            if perms_needed:
                raise PermissionDenied
            obj_display = force_unicode(obj)
            self.log_deletion(request, obj, obj_display)
            self.delete_model(request, obj)


            #### Change starts here ####
            if not '_popup' in request.REQUEST:
                self.message_user(request, _('The %(name)s "%(obj)s" was deleted successfully.') %
                                   {'name': force_unicode(opts.verbose_name), 'obj': force_unicode(obj_display)})
            return self.response_delete(request)
            #### Change ends here ####

        object_name = force_unicode(opts.verbose_name)

        if perms_needed or protected:
            title = _("Cannot delete %(name)s") % {"name": object_name}
        else:
            title = _("Are you sure?")

        context = {
            "title": title,
            "object_name": object_name,
            "object": obj,
            "deleted_objects": deleted_objects,
            "perms_lacking": perms_needed,
            "protected": protected,
            "opts": opts,
            "root_path": self.admin_site.site_url,
            "app_label": app_label,
        }
        context.update(extra_context or {})
        context_instance = template.RequestContext(request, current_app=self.admin_site.name)
        return render_to_response(self.delete_confirmation_template or [
            "admin/%s/%s/delete_confirmation.html" % (app_label, opts.object_name.lower()),
            "admin/%s/delete_confirmation.html" % app_label,
            "admin/delete_confirmation.html"
        ], context, context_instance=context_instance)


class OrderedAdminForm(helpers.AdminForm):
    
    def __init__(self, form, fieldsets, prepopulated_fields, readonly_fields=None, model_admin=None, inlines=None):
        self.inlines = inlines
        super(OrderedAdminForm, self).__init__(form, fieldsets, prepopulated_fields, readonly_fields, model_admin)

    def __iter__(self):
        for name, options in self.fieldsets:
            yield OrderedFieldset(self.form, name,
                readonly_fields=self.readonly_fields,
                model_admin=self.model_admin, inlines=self.inlines,
                **options
            )
    def first_field(self):
        try:
            fieldset_name, fieldset_options = self.fieldsets[0]
            field_name = fieldset_options['fields'][0]
            if not isinstance(field_name, basestring):
                field_name = field_name[0]
            return self.form[field_name]
        except (KeyError, IndexError):
            pass
        try:
            return iter(self.form).next()
        except StopIteration:
            return None


class OrderedFieldset(helpers.Fieldset):
    def __init__(self, form, name=None, readonly_fields=(), fields=(), classes=(),
      description=None, model_admin=None, inlines=None):
        self.inlines = inlines
        if not inlines:
            for field in fields:
                if is_inline(field):
                    # an inline is in the field list, but the list of inlines is empty
                    pass
        super(OrderedFieldset, self).__init__(form, name, readonly_fields, fields, classes, description, model_admin)

    def __iter__(self):
        for field in self.fields:
            if not is_inline(field):
                fieldline = helpers.Fieldline(self.form, field, self.readonly_fields, model_admin=self.model_admin)
                help_link = u'%s%s' % (settings.KNOWLEDGE_BASE_URL, field)
                elem = OrderedElement(fieldline=fieldline, help_link=help_link)
                yield elem
            else:
                field = decode_inline(field)
                for inline in self.inlines:
                    if hasattr(inline.opts, 'parent_fk_name'):
                        if inline.opts.parent_fk_name == field:
                            help_link = u'%s%s' % (settings.KNOWLEDGE_BASE_URL, field)
                            elem = OrderedElement(inline=inline, help_link=help_link)
                            yield elem
                    elif hasattr(inline.formset, 'prefix'):
                        if inline.formset.prefix == field:
                            elem = OrderedElement(inline=inline)
                            yield elem
                    else:
                        raise InlineError('Incorrect inline: no opts.parent_fk_name or formset.prefix found')


class OrderedElement():
    def __init__(self, fieldline=None, inline=None, help_link=None):
        if fieldline:
            self.is_field = True
            self.fieldline = fieldline
            self.help_link = help_link
        else:
            self.is_field = False
            self.inline = inline
            self.help_link = help_link


class InlineError(Exception):
    def __init__(self, msg=None):
        super(InlineError, self).__init__()
        self.msg = msg
