from django.contrib.contenttypes.models import ContentType
from django.core.urlresolvers import resolve
from django.db.models import Q
from rest_framework import generics, status
from rest_framework.generics import get_object_or_404
from rest_framework.mixins import ListModelMixin, CreateModelMixin
from rest_framework.response import Response
from oclapi.models import ResourceVersionModel


class PathWalkerMixin():
    """
    A Mixin with methods that help resolve a resource path to a resource object
    """
    path_info = None

    def get_parent_in_path(self, path_info, levels=1):
        last_index = len(path_info) - 1
        last_slash = path_info.rindex('/')
        if last_slash == last_index:
            last_slash = path_info.rindex('/', 0, last_index)
        path_info = path_info[0:last_slash+1]
        if levels > 1:
            i = 1
            while i < levels:
                last_index = len(path_info) - 1
                last_slash = path_info.rindex('/', 0, last_index)
                path_info = path_info[0:last_slash+1]
                i += 1
        return path_info

    def get_object_for_path(self, path_info, request):
        callback, callback_args, callback_kwargs = resolve(path_info)
        view = callback.cls(request=request, kwargs=callback_kwargs)
        view.initialize(request, path_info, **callback_kwargs)
        return view.get_object()


class BaseAPIView(generics.GenericAPIView):
    """
    An extension of generics.GenericAPIView that:
    1. Adds a hook for a post-initialize step
    2. De-couples the lookup field name (in the URL) from the "filter by" field name (in the queryset)
    3. Performs a soft delete on destroy()
    """
    pk_field = 'mnemonic'
    user_is_self = False

    def initial(self, request, *args, **kwargs):
        super(BaseAPIView, self).initial(request, *args, **kwargs)
        self.initialize(request, request.path_info, **kwargs)

    def initialize(self, request, path_info_segment, **kwargs):
        self.user_is_self = kwargs.pop('user_is_self', False)

    def get_object(self, queryset=None):
        # Determine the base queryset to use.
        if queryset is None:
            queryset = self.filter_queryset(self.get_queryset())
        else:
            pass  # Deprecation warning

        # Perform the lookup filtering.
        lookup = self.kwargs.get(self.lookup_field, None)
        filter_kwargs = {self.pk_field: lookup}
        obj = get_object_or_404(queryset, **filter_kwargs)

        # May raise a permission denied
        self.check_object_permissions(self.request, obj)

        return obj

    def destroy(self, request, *args, **kwargs):
        obj = self.get_object()
        obj.is_active = False
        obj.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SubResourceMixin(BaseAPIView, PathWalkerMixin):
    """
    Base view for a sub-resource.
    Includes a post-initialize step that determines the parent resource,
    and a get_queryset method that applies the appropriate permissions and filtering.
    """
    user = None
    userprofile = None
    user_is_self = False
    parent_path_info = None
    parent_resource = None
    base_or_clause = []

    def initialize(self, request, path_info_segment, **kwargs):
        super(SubResourceMixin, self).initialize(request, path_info_segment, **kwargs)
        self.user = request.user
        if self.user and hasattr(self.user, 'get_profile'):
            self.userprofile = self.user.get_profile()
        if self.user_is_self and self.userprofile:
            self.parent_resource = self.userprofile
        else:
            levels = 1 if isinstance(self, ListModelMixin) or isinstance(self, CreateModelMixin) else 2
            self.parent_path_info = self.get_parent_in_path(path_info_segment, levels=levels)
            self.parent_resource = None
            if self.parent_path_info and '/' != self.parent_path_info:
                self.parent_resource = self.get_object_for_path(self.parent_path_info, self.request)

    def get_queryset(self):
        queryset = super(SubResourceMixin, self).get_queryset()
        or_clauses = []
        if self.user:
            or_clauses.append(Q(owner=self.user))
        if self.userprofile:
            or_clauses += map(lambda x: Q(parent_id=x), self.userprofile.organizations)
        or_clauses += self.base_or_clause
        if or_clauses:
            if len(or_clauses) > 1:
                queryset = queryset.filter(reduce(lambda x, y: x | y, or_clauses[1:], or_clauses[0]))
            else:
                queryset = queryset.filter(or_clauses[0])
        if self.parent_resource:
            if hasattr(self.parent_resource, 'versioned_object'):
                self.parent_resource = self.parent_resource.versioned_object
            parent_resource_type = ContentType.objects.get_for_model(self.parent_resource)
            queryset = queryset.filter(parent_type__pk=parent_resource_type.id, parent_id=self.parent_resource.id)
        return queryset


class VersionedResourceChildMixin(SubResourceMixin):
    """
    Base view for a sub-resource that is a child of a versioned resource.
    For example, a Concept is a child of a Source, which can be versioned.
    Includes a post-initialize step that determines the parent resource,
    and a get_queryset method that limits the scope to children of the versioned resource.
    """
    parent_resource_version = None
    parent_resource_version_model = None
    child_list_attribute = None

    def initialize(self, request, path_info_segment, **kwargs):
        levels = 1 if self.model.get_url_kwarg() in kwargs else 0
        levels = levels + 1 if isinstance(self, ListModelMixin) or isinstance(self, CreateModelMixin) else levels + 2
        self.parent_path_info = self.get_parent_in_path(path_info_segment, levels=levels)
        self.parent_resource = None
        if self.parent_path_info and '/' != self.parent_path_info:
            self.parent_resource = self.get_object_for_path(self.parent_path_info, self.request)
        if hasattr(self.parent_resource, 'versioned_object'):
            self.parent_resource_version = self.parent_resource
            self.parent_resource = self.parent_resource_version.versioned_object
        else:
            self.parent_resource_version = ResourceVersionModel.get_latest_version_of(self.parent_resource)

    def get_queryset(self):
        lookup = self.kwargs.get(self.lookup_field, None)
        if lookup:
            return self.model.objects.filter(id=lookup)
        all_children = getattr(self.parent_resource_version, self.child_list_attribute) or []
        queryset = super(SubResourceMixin, self).get_queryset()
        queryset = queryset.filter(id__in=all_children)
        return queryset


class ResourceVersionMixin(BaseAPIView, PathWalkerMixin):
    """
    Base view for a resource that is a version of another resource (e.g. a SourceVersion).
    Includes a post-initialize step that determines the versioned object, and a get_queryset method
    that limits the scope to versions of that object.
    """
    versioned_object_path_info = None
    versioned_object = None

    def initialize(self, request, path_info_segment, **kwargs):
        super(ResourceVersionMixin, self).initialize(request, path_info_segment, **kwargs)
        self.versioned_object_path_info = self.get_parent_in_path(path_info_segment)
        self.versioned_object = self.get_object_for_path(self.versioned_object_path_info, request)

    def get_queryset(self):
        queryset = super(ResourceVersionMixin, self).get_queryset()
        versioned_object_type = ContentType.objects.get_for_model(self.versioned_object)
        queryset = queryset.filter(versioned_object_type__pk=versioned_object_type.id, versioned_object_id=self.versioned_object.id)
        return queryset


class ResourceAttributeChildMixin(BaseAPIView, PathWalkerMixin):
    """
    Base view for (a) child(ren) of a resource version.
    Currently, the only instances of this view are:
    GET [collection parent]/collections/:collection/:version/children
    GET [source parent]/sources/:source/:version/children
    """
    resource_version_path_info = None
    resource_version = None

    def initialize(self, request, path_info_segment, **kwargs):
        super(ResourceAttributeChildMixin, self).initialize(request, path_info_segment, **kwargs)
        self.resource_version_path_info = self.get_parent_in_path(path_info_segment)
        self.resource_version = self.get_object_for_path(self.resource_version_path_info, request)