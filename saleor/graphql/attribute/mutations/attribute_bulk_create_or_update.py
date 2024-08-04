from collections import defaultdict
from typing import Union, List, Dict

import graphene
from graphql import GraphQLError
from django.core.exceptions import ValidationError
from django.utils.text import slugify
from graphene.utils.str_converters import to_camel_case
from text_unidecode import unidecode

from ....attribute import ATTRIBUTE_PROPERTIES_CONFIGURATION, AttributeInputType, models
from ....attribute.error_codes import AttributeBulkCreateErrorCode
from ....core.tracing import traced_atomic_transaction
from ....core.utils import prepare_unique_slug
from ....permission.enums import PageTypePermissions, ProductTypePermissions
from ....webhook.event_types import WebhookEventAsyncType
from ...core import ResolveInfo
from ...core.descriptions import ADDED_IN_315, PREVIEW_FEATURE
from ...core.doc_category import DOC_CATEGORY_ATTRIBUTES
from ...core.enums import ErrorPolicyEnum
from ...core.mutations import BaseMutation, ModelMutation
from ...core.types import AttributeBulkCreateError, NonNullList
from ...core.utils import WebhookEventInfo
from ...plugins.dataloaders import get_plugin_manager_promise
from ..enums import AttributeTypeEnum
from ..mutations.attribute_create import AttributeCreateInput, AttributeValueCreateInput
from .attribute_bulk_create import AttributeBulkCreateResult, clean_values, \
    DEPRECATED_ATTR_FIELDS, get_results


class AttributeBulkCreateOrUpdate(BaseMutation):
    count = graphene.Int(
        required=True,
        description="Returns how many objects were created or updated.",
    )
    results = NonNullList(
        AttributeBulkCreateResult,
        required=True,
        default_value=[],
        description="List of the created or updated attributes.",
    )

    class Arguments:
        attributes = NonNullList(
            AttributeCreateInput,
            required=True,
            description="Input list of attributes to create or update.",
        )
        error_policy = ErrorPolicyEnum(
            required=False,
            description="Policies of error handling. DEFAULT: "
                        + ErrorPolicyEnum.REJECT_EVERYTHING.name,
        )

    class Meta:
        description = "Creates or updates attributes." + ADDED_IN_315 + PREVIEW_FEATURE
        doc_category = DOC_CATEGORY_ATTRIBUTES
        error_type_class = AttributeBulkCreateError
        webhook_events_info = [
            WebhookEventInfo(
                type=WebhookEventAsyncType.ATTRIBUTE_CREATED,
                description="An attribute was created.",
            ),
            WebhookEventInfo(
                type=WebhookEventAsyncType.ATTRIBUTE_UPDATED,
                description="An attribute was updated.",
            ),
        ]

    @classmethod
    def clean_attribute_input(
        cls,
        info: ResolveInfo,
        attribute_data: AttributeCreateInput,
        attribute_index: int,
        existing_slugs: set,
        values_existing_external_refs: set,
        duplicated_values_external_ref: set,
        index_error_map: dict[int, list[AttributeBulkCreateError]],
    ) -> Dict:
        values = attribute_data.pop("values", None)
        cleaned_input = ModelMutation.clean_input(
            info, None, attribute_data, input_cls=AttributeCreateInput
        )

        # check permissions based on attribute type
        permissions: Union[tuple[ProductTypePermissions], tuple[PageTypePermissions]]
        if cleaned_input["type"] == AttributeTypeEnum.PRODUCT_TYPE.value:
            permissions = (ProductTypePermissions.MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES,)
        else:
            permissions = (PageTypePermissions.MANAGE_PAGE_TYPES_AND_ATTRIBUTES,)

        if not cls.check_permissions(info.context, permissions):
            index_error_map[attribute_index].append(
                AttributeBulkCreateError(
                    message=(
                        "You have no permission to manage this type of attributes. "
                        f"You need one of the following permissions: {permissions}"
                    ),
                    code=AttributeBulkCreateErrorCode.REQUIRED.value,
                )
            )
            return None

        input_type = cleaned_input.get("input_type")
        entity_type = cleaned_input.get("entity_type")
        if input_type == AttributeInputType.REFERENCE and not entity_type:
            index_error_map[attribute_index].append(
                AttributeBulkCreateError(
                    path="entityType",
                    message=(
                        "Entity type is required when REFERENCE input type is used."
                    ),
                    code=AttributeBulkCreateErrorCode.REQUIRED.value,
                )
            )
            return None

        # check attribute configuration
        for field in ATTRIBUTE_PROPERTIES_CONFIGURATION.keys():
            allowed_input_type = ATTRIBUTE_PROPERTIES_CONFIGURATION[field]

            if input_type not in allowed_input_type and cleaned_input.get(field):
                camel_case_field = to_camel_case(field)
                index_error_map[attribute_index].append(
                    AttributeBulkCreateError(
                        path=camel_case_field,
                        message=(
                            f"Cannot set {camel_case_field} on a {input_type} "
                            "attribute.",
                        ),
                        code=AttributeBulkCreateErrorCode.INVALID.value,
                    )
                )
                return None

        # generate slug
        cleaned_input["slug"] = cls._generate_slug(cleaned_input, existing_slugs)

        if values:
            cleaned_values = clean_values(
                values,
                input_type,
                values_existing_external_refs,
                duplicated_values_external_ref,
                attribute_index,
                index_error_map,
            )
            cleaned_input["values"] = cleaned_values

        return cleaned_input

    @classmethod
    def clean_attributes(
        cls,
        info: ResolveInfo,
        attributes_data: List[AttributeCreateInput],
        index_error_map: Dict[int, List[AttributeBulkCreateError]],
    ) -> Dict[int, Dict]:
        cleaned_inputs_map = {}

        existing_slugs = set(
            models.Attribute.objects.filter(
                slug__in=[attribute_data.slug for attribute_data in attributes_data]
            ).values_list("slug", flat=True)
        )

        # Prepare external refs and other variables
        values_existing_external_refs = set()
        duplicated_values_external_ref = set()

        for attribute_index, attribute_data in enumerate(attributes_data):
            slug = attribute_data.slug
            if not slug:
                index_error_map[attribute_index].append(
                    AttributeBulkCreateError(
                        path="slug",
                        message="Slug is required for create or update operation.",
                        code=AttributeBulkCreateErrorCode.REQUIRED.value,
                    )
                )
                cleaned_inputs_map[attribute_index] = None
                continue

            if slug in existing_slugs:
                cleaned_inputs_map[attribute_index] = attribute_data
                continue

            if any(key in DEPRECATED_ATTR_FIELDS for key in attribute_data.keys()):
                message = (
                    "Deprecated fields 'storefront_search_position', "
                    "'filterable_in_storefront', 'available_in_grid' are not "
                    "allowed in bulk mutation."
                )
                index_error_map[attribute_index].append(
                    AttributeBulkCreateError(
                        message=message,
                        code=AttributeBulkCreateErrorCode.INVALID.value,
                    )
                )
                cleaned_inputs_map[attribute_index] = None
                continue

            cleaned_input = cls.clean_attribute_input(
                info,
                attribute_data,
                attribute_index,
                existing_slugs,
                values_existing_external_refs,
                duplicated_values_external_ref,
                index_error_map
            )
            # Process values here if they exist
            values_data = attribute_data.get("values", [])
            cleaned_input["values"] = [
                cls.clean_value_input(info, value_data, attribute_index,
                                      index_error_map)
                for value_data in values_data
            ]

            cleaned_inputs_map[attribute_index] = cleaned_input

        return cleaned_inputs_map

    @classmethod
    def clean_value_input(
        cls,
        info: ResolveInfo,
        value_data: AttributeValueCreateInput,
        attribute_index: int,
        index_error_map: Dict[int, List[AttributeBulkCreateError]],
    ) -> Dict:
        cleaned_input = ModelMutation.clean_input(
            info, None, value_data, input_cls=AttributeValueCreateInput
        )

        errors = {}
        if errors:
            index_error_map[attribute_index].append(
                AttributeBulkCreateError(
                    path=f"values.{value_data.get('index')}",
                    message=" ".join(e.message for e in errors.values()),
                    code=AttributeBulkCreateErrorCode.INVALID.value,
                )
            )
            return None

        return cleaned_input

    @classmethod
    def _generate_slug(cls, cleaned_input, existing_slugs):
        slug = cleaned_input.get("slug")
        unique_slug = prepare_unique_slug(slug, existing_slugs)
        existing_slugs.add(unique_slug)
        return unique_slug

    @classmethod
    def create_or_update_attributes(
        cls,
        info: ResolveInfo,
        cleaned_inputs_map: Dict[int, Dict],
        error_policy: str,
        index_error_map: Dict[int, List[AttributeBulkCreateError]],
    ) -> List[Dict]:
        instances_data_and_errors_list = []

        for index, cleaned_input in cleaned_inputs_map.items():
            if not cleaned_input:
                instances_data_and_errors_list.append(
                    {"instance": None, "errors": index_error_map[index]}
                )
                continue

            slug = cleaned_input["slug"]
            instance, created = models.Attribute.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": cleaned_input["name"],
                    "type": cleaned_input["type"],
                    "input_type": cleaned_input["input_type"],
                }
            )

            if created:
                webhook_event_type = WebhookEventAsyncType.ATTRIBUTE_CREATED
            else:
                webhook_event_type = WebhookEventAsyncType.ATTRIBUTE_UPDATED

            instances_data_and_errors_list.append(
                {"instance": instance, "errors": index_error_map[index]}
            )

            # Handle attribute values if they are provided
            values_data = cleaned_input.get("values", [])
            if values_data:
                cls.create_values(instance, values_data, index_error_map, index)

        if error_policy == ErrorPolicyEnum.REJECT_FAILED_ROWS.value:
            for instance_data in instances_data_and_errors_list:
                if instance_data["errors"]:
                    instance_data["instance"] = None

        return instances_data_and_errors_list

    @staticmethod
    def check_generation_slug(
        name=None,
        ref=None,
        value=None,
        code=None
    ) -> str:
        """Generate a slug and check its uniqueness in the database.

        Args:
            name: the name to use for slug generation
            ref: optional reference value
            value: optional value used with ref to create slug
            code: optional code value

        Returns:
            A unique slug.
        """
        # Determine the slug base
        if ref and value:
            slug_base = f"{ref}-{value}"
        elif name and code:
            slug_base = f"{name}-{code}"
        else:
            slug_base = name or code or "-"

        # Generate the slug using slugify and unidecode
        slug = slugify(unidecode(slug_base))

        # Provide a default value if slug is empty
        if not slug:
            slug = "-"

        return slug

    @classmethod
    def create_values(
        cls,
        attribute: models.Attribute,
        values_data: List[Dict],
        index_error_map: Dict[int, List[AttributeBulkCreateError]],
        attr_index: int
    ):
        values_to_create = []
        values_to_update = []

        value_slugs = {}
        for value_data in values_data:
            value_slug = cls.check_generation_slug(
                name=value_data.get("name"),
                ref=value_data.get("additional_fields", {}).get("ref"),
                value=value_data.get("additional_fields", {}).get("value"),
                code=value_data.get("additional_fields", {}).get("code")
            )
            value_slugs[value_slug] = value_data

        existing_values_slugs = models.AttributeValue.objects.filter(
            attribute=attribute,
            slug__in=value_slugs.keys()
        ).values_list("slug", flat=True)

        for value_slug, value_data in value_slugs.items():
            value_name = value_data.get("name", "")
            value_ref = value_data.get("additional_fields", {}).get("ref")
            value_field = value_data.get("additional_fields", {}).get("value")
            value_code = value_data.get("additional_fields", {}).get("code")

            if value_field is None:
                value_field = ""

            value = models.AttributeValue(attribute=attribute)
            value = cls.construct_instance(value, value_data)
            value.slug = value_slug

            try:
                if value_slug in existing_values_slugs:
                    existing_value = models.AttributeValue.objects.get(
                        attribute=attribute,
                        slug=value_slug
                    )
                    existing_value.name = value_name
                    existing_value.ref = value_ref
                    existing_value.value = value_field
                    existing_value.code = value_code
                    existing_value.full_clean(exclude=["attribute", "slug"])
                    values_to_update.append(existing_value)
                else:
                    value.name = value_name
                    value.ref = value_ref
                    value.value = value_field
                    value.code = value_code
                    value.full_clean(exclude=["attribute", "slug"])
                    values_to_create.append(value)

            except ValidationError as exc:
                for key, errors in exc.error_dict.items():
                    for e in errors:
                        path = f"values.{value_data.get('index')}.{to_camel_case(key)}"
                        index_error_map[attr_index].append(
                            AttributeBulkCreateError(
                                path=path,
                                message=e.messages[0],
                                code=e.code,
                            )
                        )

        if values_to_create:
            models.AttributeValue.objects.bulk_create(values_to_create)

        if values_to_update:
            models.AttributeValue.objects.bulk_update(values_to_update,
                                                      ["name", "additional_fields"])

    @classmethod
    def save(
        cls, instances_data_with_errors_list: List[Dict]
    ) -> List[models.Attribute]:
        attributes_to_save = []
        for attribute_data in instances_data_with_errors_list:
            attribute = attribute_data["instance"]
            if attribute:
                attributes_to_save.append(attribute)

        models.Attribute.objects.bulk_update(
            attributes_to_save, ["name", "type", "input_type"]
        )

        return attributes_to_save

    @classmethod
    def post_save_actions(cls, info: ResolveInfo, attributes: List[models.Attribute]):
        manager = get_plugin_manager_promise(info.context).get()
        for attribute in attributes:
            cls.call_event(
                manager.attribute_created if attribute.pk is None else manager.attribute_updated,
                attribute)

    @classmethod
    @traced_atomic_transaction()
    def perform_mutation(cls, root, info, **data):
        if not cls.check_permissions(info.context, (
            ProductTypePermissions.MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES,
            PageTypePermissions.MANAGE_PAGE_TYPES_AND_ATTRIBUTES)):
            raise GraphQLError(
                "You do not have the necessary permissions to perform this operation.")

        index_error_map = defaultdict(list)
        error_policy = data.get("error_policy", ErrorPolicyEnum.REJECT_EVERYTHING.value)

        # Clean and validate inputs
        cleaned_inputs_map = cls.clean_attributes(
            info, data["attributes"], index_error_map
        )
        instances_data_with_errors_list = cls.create_or_update_attributes(
            info, cleaned_inputs_map, error_policy, index_error_map
        )

        # Check if errors occurred
        inputs_have_errors = any(
            errors for errors in index_error_map.values()
        )

        if (
            inputs_have_errors
            and error_policy == ErrorPolicyEnum.REJECT_EVERYTHING.value
        ):
            results = get_results(instances_data_with_errors_list, True)
            return AttributeBulkCreateOrUpdate(count=0, results=results)

        # Save all objects
        attributes = cls.save(instances_data_with_errors_list)

        # Prepare and return data
        results = get_results(instances_data_with_errors_list)
        cls.post_save_actions(info, attributes)

        return AttributeBulkCreateOrUpdate(count=len(attributes), results=results)
