from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Optional, Union, cast
from uuid import UUID

from django.db.models import Exists, F, OuterRef
from django.utils import timezone
from prices import Money

from ... import settings
from ...channel.models import Channel
from ...core.db.connection import allow_writer
from ...core.taxes import zero_money
from ...core.utils.promo_code import InvalidPromoCode
from ...order.models import Order, OrderLine
from .. import DiscountType, VoucherType
from ..interface import DiscountInfo, VoucherInfo
from ..models import (
    DiscountValueType,
    NotApplicable,
    OrderLineDiscount,
    Voucher,
    VoucherCode,
    VoucherCustomer,
)
from .manual_discount import apply_discount_to_value
from .shared import update_discount

if TYPE_CHECKING:
    from ...account.models import User
    from ...checkout.fetch import CheckoutInfo, CheckoutLineInfo
    from ...checkout.models import Checkout
    from ...core.pricing.interface import LineInfo
    from ...order.fetch import EditableOrderLineInfo
    from ...plugins.manager import PluginsManager
    from ..models import Voucher


@dataclass
class VoucherDenormalizedInfo:
    discount_value: Decimal
    discount_value_type: str
    voucher_type: str
    reason: str | None
    name: str | None
    apply_once_per_order: bool
    origin_line_ids: list[UUID]


def is_order_level_voucher(voucher: Voucher | None):
    return bool(
        voucher
        and voucher.type == VoucherType.ENTIRE_ORDER
        and not voucher.apply_once_per_order
    )


def is_shipping_voucher(voucher: Voucher | None):
    return bool(voucher and voucher.type == VoucherType.SHIPPING)


def is_line_level_voucher(voucher: Voucher | None):
    return voucher and (
        voucher.type == VoucherType.SPECIFIC_PRODUCT or voucher.apply_once_per_order
    )


def increase_voucher_usage(
    voucher: "Voucher",
    code: "VoucherCode",
    customer_email: str | None,
    increase_voucher_customer_usage: bool = True,
) -> None:
    if voucher.usage_limit:
        increase_voucher_code_usage_value(code)
    if voucher.apply_once_per_customer and increase_voucher_customer_usage:
        add_voucher_usage_by_customer(code, customer_email)
    if voucher.single_use:
        deactivate_voucher_code(code)


def increase_voucher_code_usage_value(code: "VoucherCode") -> None:
    """Increase voucher code uses by 1."""
    code.used = F("used") + 1
    code.save(update_fields=["used"])


def decrease_voucher_code_usage_value(code: "VoucherCode") -> None:
    """Decrease voucher code uses by 1."""
    code.used = F("used") - 1
    code.save(update_fields=["used"])


def deactivate_voucher_code(code: "VoucherCode") -> None:
    """Mark voucher code as used."""
    code.is_active = False
    code.save(update_fields=["is_active"])


def activate_voucher_code(code: "VoucherCode") -> None:
    """Mark voucher code as unused."""
    code.is_active = True
    code.save(update_fields=["is_active"])


def add_voucher_usage_by_customer(
    code: "VoucherCode", customer_email: str | None
) -> None:
    if not customer_email:
        raise NotApplicable("Unable to apply voucher as customer details are missing.")

    _, created = VoucherCustomer.objects.get_or_create(
        voucher_code=code, customer_email=customer_email
    )
    if not created:
        raise NotApplicable("This offer is only valid once per customer.")


def remove_voucher_usage_by_customer(code: "VoucherCode", customer_email: str) -> None:
    voucher_customer = VoucherCustomer.objects.filter(
        voucher_code=code, customer_email=customer_email
    )
    if voucher_customer:
        voucher_customer.delete()


def release_voucher_code_usage(
    code: Optional["VoucherCode"],
    voucher: Optional["Voucher"],
    user_email: str | None,
):
    if not code:
        return
    if voucher and voucher.usage_limit:
        decrease_voucher_code_usage_value(code)
    if voucher and voucher.single_use:
        activate_voucher_code(code)
    if user_email:
        remove_voucher_usage_by_customer(code, user_email)


def get_voucher_code_instance(
    voucher_code: str,
    channel_slug: str,
):
    """Return a voucher code instance if it's valid or raise an error."""
    if (
        Voucher.objects.active_in_channel(
            date=timezone.now(), channel_slug=channel_slug
        )
        .filter(
            Exists(
                VoucherCode.objects.filter(
                    code=voucher_code,
                    voucher_id=OuterRef("id"),
                    is_active=True,
                )
            )
        )
        .exists()
    ):
        code_instance = VoucherCode.objects.get(code=voucher_code)
    else:
        raise InvalidPromoCode()
    return code_instance


def get_active_voucher_code(voucher, channel_slug):
    """Return an active VoucherCode instance.

    This method along with `Voucher.code` should be removed in Saleor 4.0.
    """

    voucher_queryset = Voucher.objects.active_in_channel(timezone.now(), channel_slug)
    if not voucher_queryset.filter(pk=voucher.pk).exists():
        raise InvalidPromoCode()
    voucher_code = VoucherCode.objects.filter(voucher=voucher, is_active=True).first()
    if not voucher_code:
        raise InvalidPromoCode()
    return voucher_code


def attach_voucher_to_line_info(
    voucher_info: "VoucherInfo",
    lines_info: Sequence["LineInfo"],
):
    """Attach voucher to valid checkout or order lines info.

    Apply a voucher to checkout/order line info when the voucher has the type
    SPECIFIC_PRODUCTS or is applied only to the cheapest item.
    """
    voucher = voucher_info.voucher
    discounted_lines_by_voucher: list[LineInfo] = []
    lines_included_in_discount = lines_info
    if voucher.type == VoucherType.SPECIFIC_PRODUCT:
        discounted_lines_by_voucher.extend(
            get_discounted_lines(lines_info, voucher_info)
        )
        lines_included_in_discount = discounted_lines_by_voucher
    if voucher.apply_once_per_order:
        if cheapest_line := get_the_cheapest_line(lines_included_in_discount):
            discounted_lines_by_voucher = [cheapest_line]
    for line_info in lines_info:
        if line_info in discounted_lines_by_voucher:
            line_info.voucher = voucher
            line_info.voucher_code = voucher_info.voucher_code


def get_discounted_lines(
    lines: Iterable["LineInfo"], voucher_info: "VoucherInfo"
) -> Iterable["LineInfo"]:
    discounted_lines = []
    if (
        voucher_info.product_pks
        or voucher_info.collection_pks
        or voucher_info.category_pks
        or voucher_info.variant_pks
    ):
        for line_info in lines:
            if line_info.line.is_gift:
                continue
            line_variant = line_info.variant
            line_product = line_info.product
            if not line_variant or not line_product:
                continue
            line_category = line_product.category
            line_collections = {
                collection.pk for collection in line_info.collections if collection
            }
            if line_info.variant and (
                line_variant.pk in voucher_info.variant_pks
                or line_product.pk in voucher_info.product_pks
                or line_category
                and line_category.pk in voucher_info.category_pks
                or line_collections.intersection(voucher_info.collection_pks)
            ):
                discounted_lines.append(line_info)
    else:
        # If there's no discounted products, collections or categories,
        # it means that all products are discounted
        discounted_lines.extend(lines)
    return discounted_lines


def get_the_cheapest_line(
    lines_info: Iterable["LineInfo"] | None,
) -> Optional["LineInfo"]:
    if not lines_info:
        return None
    return min(lines_info, key=lambda line_info: line_info.variant_discounted_price)


def get_customer_email_for_voucher_usage(
    source_object: Union["Order", "Checkout", "CheckoutInfo"],
):
    """Get customer email for voucher.

    Always prioritize the user's email over the email assigned to the
    source object in terms of voucher application.
    If the source object has associated user, return the user's email.
    Otherwise, return the customer email from the source object.
    """

    if source_object.user:
        user = cast("User", source_object.user)
        return user.email
    return source_object.get_customer_email()


def validate_voucher_for_checkout(
    manager: "PluginsManager",
    voucher: "Voucher",
    checkout_info: "CheckoutInfo",
    lines: list["CheckoutLineInfo"],
):
    from ...checkout import base_calculations
    from ...checkout.utils import calculate_checkout_quantity

    quantity = calculate_checkout_quantity(lines)
    subtotal = base_calculations.base_checkout_subtotal(
        lines,
        checkout_info.channel,
        checkout_info.checkout.currency,
    )

    customer_email = cast(str, get_customer_email_for_voucher_usage(checkout_info))
    validate_voucher(
        voucher,
        subtotal,
        quantity,
        customer_email,
        checkout_info.channel,
        checkout_info.user,
    )


def validate_voucher_in_order(
    order: "Order", lines: Iterable["OrderLine"], channel: "Channel"
):
    if not order.voucher:
        return

    from ...order.utils import get_total_quantity

    subtotal = order.subtotal
    quantity = get_total_quantity(lines)
    customer_email = get_customer_email_for_voucher_usage(order)
    tax_configuration = channel.tax_configuration
    prices_entered_with_tax = tax_configuration.prices_entered_with_tax
    value = subtotal.gross if prices_entered_with_tax else subtotal.net

    validate_voucher(
        order.voucher, value, quantity, customer_email, channel, order.user
    )


def validate_voucher(
    voucher: "Voucher",
    total_price: Money,
    quantity: int,
    customer_email: str,
    channel: Channel,
    customer: Optional["User"],
) -> None:
    voucher.validate_min_spent(total_price, channel)
    voucher.validate_min_checkout_items_quantity(quantity)
    if voucher.apply_once_per_customer:
        voucher.validate_once_per_customer(customer_email)
    if voucher.only_for_staff:
        voucher.validate_only_for_staff(customer)


def get_products_voucher_discount(
    voucher: "Voucher", prices: Iterable[Money], channel: Channel
) -> Money:
    """Calculate discount value for a voucher of product or category type."""
    if voucher.apply_once_per_order:
        return voucher.get_discount_amount_for(min(prices), channel)
    discounts = (voucher.get_discount_amount_for(price, channel) for price in prices)
    total_amount = sum(discounts, zero_money(channel.currency_code))
    return total_amount


def create_or_update_voucher_discount_objects_for_order(
    order: "Order", use_denormalized_data=False
):
    """Handle voucher discount objects for order.

    Take into account all the mutual dependence and exclusivity between various types of
    discounts.

    `use_denormalized_data=True` indicates, that the discount should be calculated based on the
    conditions from the moment of the voucher application. Otherwise, the latest
    voucher values will be retrieved from the database.
    """
    from ...order.fetch import fetch_draft_order_lines_info
    from ...order.models import OrderLine

    create_or_update_discount_object_from_order_level_voucher(order)
    lines_info = fetch_draft_order_lines_info(order)
    create_or_update_line_discount_objects_from_voucher(
        lines_info, use_denormalized_data
    )
    lines = [line_info.line for line_info in lines_info]
    OrderLine.objects.bulk_update(
        lines,
        [
            "base_unit_price_amount",
            "unit_discount_amount",
            "unit_discount_reason",
            "unit_discount_type",
            "unit_discount_value",
        ],
    )


def create_or_update_discount_object_from_order_level_voucher(
    order,
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
):
    """Create or update discount object for ENTIRE_ORDER and SHIPPING voucher."""
    voucher = order.voucher

    # The order-level voucher discount should be deleted when:
    # - order.voucher_id is None
    # - manual order-level discount exists together with order-level voucher
    # - the order has line-level voucher attached
    is_manual_discount = order.discounts.filter(type=DiscountType.MANUAL).exists()
    is_order_voucher = is_order_level_voucher(voucher)
    is_line_level_voucher = not is_order_voucher and not is_shipping_voucher(voucher)
    should_delete_order_level_voucher_discount = (
        not order.voucher_id
        or (is_order_voucher and is_manual_discount)
        or is_line_level_voucher
    )

    if should_delete_order_level_voucher_discount:
        with allow_writer():
            order.discounts.filter(type=DiscountType.VOUCHER).delete()
            if not is_shipping_voucher(voucher):
                order.base_shipping_price = order.undiscounted_base_shipping_price
            return

    voucher_channel_listing = (
        voucher.channel_listings.using(database_connection_name)
        .filter(channel=order.channel)
        .first()
    )
    if not voucher_channel_listing:
        return

    discount_amount = zero_money(order.currency)
    if is_order_level_voucher(voucher):
        discount_amount = voucher.get_discount_amount_for(
            order.subtotal.net, order.channel
        )

    if is_shipping_voucher(voucher):
        discount_amount = voucher.get_discount_amount_for(
            order.undiscounted_base_shipping_price, order.channel
        )
        # Shipping voucher is tricky: it is associated with an order, but it
        # decreases base price, similar to line level discounts
        order.base_shipping_price = max(
            order.undiscounted_base_shipping_price - discount_amount,
            zero_money(order.currency),
        )
    else:
        order.base_shipping_price = order.undiscounted_base_shipping_price

    discount_reason = f"Voucher code: {order.voucher_code}"
    discount_name = voucher.name or ""

    discount_data = DiscountInfo(
        voucher=voucher,
        value_type=voucher.discount_value_type,
        value=voucher_channel_listing.discount_value,
        amount_value=discount_amount.amount,
        currency=order.currency,
        reason=discount_reason,
        name=discount_name,
        type=DiscountType.VOUCHER,
        voucher_code=order.voucher_code,
        # TODO (SHOPX-914): set translated voucher name
        translated_name="",
    )

    with allow_writer():
        discount_object, created = order.discounts.get_or_create(
            type=DiscountType.VOUCHER,
            defaults=asdict(discount_data),
        )
        if not created:
            updated_fields: list[str] = []
            update_discount(
                rule=None,
                voucher=voucher,
                discount_name=discount_name,
                # TODO (SHOPX-914): set translated voucher name
                translated_name="",
                discount_reason=discount_reason,
                discount_amount=discount_amount.amount,
                value=voucher_channel_listing.discount_value,
                value_type=voucher.discount_value_type,
                unique_type=DiscountType.VOUCHER,
                discount_to_update=discount_object,
                updated_fields=updated_fields,
                voucher_code=order.voucher_code,
            )
            if updated_fields:
                discount_object.save(update_fields=updated_fields)


def create_or_update_line_discount_objects_from_voucher(
    lines_info, use_denormalized_data=False
):
    """Create or update line discount object for voucher applied on lines.

    The LineDiscount object is created for each line with voucher applied.
    Only `SPECIFIC_PRODUCT` and `apply_once_per_order` voucher types are applied.

    `use_denormalized_data=True` indicates, that the discount should be calculated based
    on the conditions from the moment of the voucher application. Otherwise, the latest
    voucher values will be retrieved from the database.
    """
    # FIXME: temporary - create_order_line_discount_objects should be moved to shared
    from .order import (
        create_order_line_discount_objects,
        update_unit_discount_data_on_order_lines_info,
    )

    discount_data = prepare_line_discount_objects_for_voucher(
        lines_info, use_denormalized_data
    )
    modified_lines_info = create_order_line_discount_objects(lines_info, discount_data)
    if modified_lines_info:
        _reduce_base_unit_price_for_voucher_discount(modified_lines_info)
        update_unit_discount_data_on_order_lines_info(modified_lines_info)


# TODO (SHOPX-912): share the method with checkout
def prepare_line_discount_objects_for_voucher(
    lines_info: list["EditableOrderLineInfo"],
    use_denormalized_data=False,
):
    """Prepare line-level voucher discount objects to be created, updated and deleted.

    `use_denormalized_data=True` indicates, that the discount should be calculated based on the
    conditions from the moment of the voucher application. Otherwise, the latest
    voucher values will be retrieved from the database.
    """
    line_discounts_to_create: list[OrderLineDiscount] = []
    line_discounts_to_update: list[OrderLineDiscount] = []
    line_discounts_to_remove: list[OrderLineDiscount] = []
    updated_fields: list[str] = []

    if not lines_info:
        return None

    for line_info in lines_info:
        line = line_info.line
        voucher = cast(Voucher, line_info.voucher)
        total_price = line_info.variant_discounted_price * line.quantity

        # only one voucher can be applied
        discount_to_update = None
        if discounts_to_update := line_info.get_voucher_discounts():
            discount_to_update = discounts_to_update[0]

        # manual line discount do not stack with other line discounts
        manual_line_discount = line_info.get_manual_line_discount()

        if (
            (not voucher and use_denormalized_data is False)
            or line.is_gift
            or manual_line_discount
        ):
            if discount_to_update:
                line_discounts_to_remove.append(discount_to_update)
            continue

        if use_denormalized_data:
            if not line_info.voucher_denormalized_info:
                if discount_to_update:
                    line_discounts_to_remove.append(discount_to_update)
                continue

            discount_amount = (
                calculate_order_line_discount_amount_from_denormalized_voucher(
                    line_info, total_price
                )
            )
            voucher_denormalized_info = line_info.voucher_denormalized_info
            discount_name = f"{voucher_denormalized_info.name}"
            discount_value = voucher_denormalized_info.discount_value
            discount_value_type = voucher_denormalized_info.discount_value_type
        else:
            discount_amount = calculate_line_discount_amount_from_voucher(
                line_info, total_price
            )
            voucher_listing = voucher.channel_listings.get(channel=line_info.channel)
            discount_name = f"{voucher.name}"
            discount_value = voucher_listing.discount_value
            discount_value_type = voucher.discount_value_type

        discount_amount = discount_amount.amount
        code = line_info.voucher_code
        discount_reason = f"Voucher code: {code}"

        if discount_to_update:
            update_discount(
                rule=None,
                voucher=voucher,
                discount_name=discount_name,
                # TODO (SHOPX-914): set translated voucher name
                translated_name="",
                discount_reason=discount_reason,
                discount_amount=discount_amount,
                value=discount_value,
                value_type=discount_value_type,
                unique_type=DiscountType.VOUCHER,
                discount_to_update=discount_to_update,
                updated_fields=updated_fields,
                voucher_code=code,
            )
            line_discounts_to_update.append(discount_to_update)
        else:
            line_discount_to_create = OrderLineDiscount(
                line=line,
                type=DiscountType.VOUCHER,
                value_type=discount_value_type,
                value=discount_value,
                amount_value=discount_amount,
                currency=line.currency,
                name=discount_name,
                translated_name=None,
                reason=discount_reason,
                voucher=voucher,
                unique_type=DiscountType.VOUCHER,
                voucher_code=code,
            )
            line_discounts_to_create.append(line_discount_to_create)

    return (
        line_discounts_to_create,
        line_discounts_to_update,
        line_discounts_to_remove,
        updated_fields,
    )


def calculate_line_discount_amount_from_voucher(
    line_info: "LineInfo", total_price: Money
) -> Money:
    """Calculate discount amount for voucher applied on line.

    Included vouchers: `SPECIFIC_PRODUCT` and `apply_once_per_order`.

    Args:
        line_info: Order/Checkout line data.
        total_price: Total price of the line, should be already reduced by
    catalogue discounts if any applied.

    """
    if not line_info.voucher:
        return zero_money(total_price.currency)

    channel = line_info.channel
    quantity = line_info.line.quantity
    if not line_info.voucher.apply_once_per_order:
        if line_info.voucher.discount_value_type == DiscountValueType.PERCENTAGE:
            voucher_discount_amount = line_info.voucher.get_discount_amount_for(
                total_price, channel=channel
            )
            discount_amount = min(voucher_discount_amount, total_price)
        else:
            unit_price = total_price / quantity
            voucher_unit_discount_amount = line_info.voucher.get_discount_amount_for(
                unit_price, channel=channel
            )
            discount_amount = min(
                voucher_unit_discount_amount * quantity,
                total_price,
            )
    else:
        unit_price = total_price / quantity
        voucher_unit_discount_amount = line_info.voucher.get_discount_amount_for(
            unit_price, channel=channel
        )
        discount_amount = min(voucher_unit_discount_amount, unit_price)
    return discount_amount


def calculate_order_line_discount_amount_from_denormalized_voucher(
    line_info: "EditableOrderLineInfo", total_price: Money
) -> Money:
    """Calculate discount amount for line-level vouchers with denormalized values."""
    voucher_info = line_info.voucher_denormalized_info
    if not voucher_info:
        return zero_money(total_price.currency)

    quantity = line_info.line.quantity
    currency = total_price.currency
    if not voucher_info.apply_once_per_order:
        if voucher_info.discount_value_type == DiscountValueType.PERCENTAGE:
            discounted_total_price = apply_discount_to_value(
                value=voucher_info.discount_value,
                value_type=voucher_info.discount_value_type,
                currency=currency,
                price_to_discount=total_price,
            )
            discount_amount = min(total_price - discounted_total_price, total_price)
        else:
            unit_price = Money(total_price.amount / quantity, currency)
            discounted_unit_price = apply_discount_to_value(
                value=voucher_info.discount_value,
                value_type=voucher_info.discount_value_type,
                currency=currency,
                price_to_discount=unit_price,
            )
            voucher_unit_discount_amount = max(
                unit_price - discounted_unit_price, zero_money(unit_price.currency)
            )
            discount_amount = min(voucher_unit_discount_amount * quantity, total_price)
    else:
        unit_price = Money(total_price.amount / quantity, currency)
        discounted_unit_price = apply_discount_to_value(
            value=voucher_info.discount_value,
            value_type=voucher_info.discount_value_type,
            currency=currency,
            price_to_discount=unit_price,
        )
        voucher_unit_discount_amount = max(
            unit_price - discounted_unit_price, zero_money(unit_price.currency)
        )
        # vouchers applicable once per order discounts only a single unit of the line
        discount_amount = min(voucher_unit_discount_amount, unit_price)
    return discount_amount


def _reduce_base_unit_price_for_voucher_discount(
    lines_info: list["EditableOrderLineInfo"],
):
    for line_info in lines_info:
        line = line_info.line
        base_unit_price = line_info.variant_discounted_price.amount
        for discount in line_info.get_voucher_discounts():
            base_unit_price -= discount.amount_value / line.quantity
        line.base_unit_price_amount = max(base_unit_price, Decimal(0))
