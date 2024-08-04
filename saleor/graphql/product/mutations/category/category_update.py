import graphene
from graphql import GraphQLError

from ....core import ResolveInfo
from ....core.types import ProductError
from ....plugins.dataloaders import get_plugin_manager_promise
from .....permission.enums import ProductPermissions
from .....product import models
from ...types import Category
from .category_create import CategoryInput, BaseCategoryMutation


class CategoryUpdate(BaseCategoryMutation):
    class Arguments:
        id = graphene.ID(required=False, description="ID of a category to update.")
        slug = graphene.String(required=False, description="Slug of a category to update.")
        input = CategoryInput(required=True, description="Fields required to update a category.")
        parent_id = graphene.ID(description="ID of the parent category. If empty, category will be top level category.", name="parent_id")
        parent_slug = graphene.String(description="Slug of the parent category. If empty, category will be top level category.", name="parent_slug")

    class Meta:
        description = "Updates a category."
        model = models.Category
        object_type = Category
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"
        support_meta_field = True
        support_private_meta_field = True

    @classmethod
    def get_instance(cls, info: ResolveInfo, **data):
        category_id = data.get("id")
        category_slug = data.get("slug")

        if category_id:
            return cls.get_node_or_error(info, category_id, field="id", only_type=Category)
        elif category_slug:
            try:
                return models.Category.objects.get(slug=category_slug)
            except models.Category.DoesNotExist:
                raise GraphQLError(f"Category with slug '{category_slug}' not found.")
        else:
            raise GraphQLError("Either 'id' or 'slug' must be provided.")

    @classmethod
    def perform_mutation(cls, root, info: ResolveInfo, /, **data):
        instance = cls.get_instance(info, **data)
        data["input"]["parent_id"] = data.pop("parent_id", None)
        data["input"]["parent_slug"] = data.pop("parent_slug", None)
        return super().perform_mutation(root, info, instance=instance, **data)

    @classmethod
    def post_save_action(cls, info: ResolveInfo, instance, _cleaned_input):
        manager = get_plugin_manager_promise(info.context).get()
        cls.call_event(manager.category_updated, instance)
